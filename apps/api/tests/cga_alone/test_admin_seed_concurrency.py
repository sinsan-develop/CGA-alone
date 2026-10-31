"""S13 실제 PostgreSQL 두 transaction의 기본 Admin 초기화. 서비스 DB/권한/Provider는 변경하지 않는다."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4
import json
import pytest
from sqlalchemy import select, delete, event, func, text
from sqlalchemy.orm import Session
from app.models import Organization, AdminChannel, AdminTemplate
from app.api.routes import admin


@pytest.fixture
def seed_context(test_engine, migrated_test_database):
    assert test_engine.dialect.name == "postgresql"
    orgs=[];receipt=None
    with Session(test_engine) as db:
        for _ in range(2):
            org=Organization(code="s13-seed-"+uuid4().hex,name="S13 초기화 검증");db.add(org);db.flush();orgs.append(org.id)
        directory=Path(__file__).resolve().parents[4]/".local/s13-fixture-owners";directory.mkdir(parents=True,exist_ok=True)
        receipt=directory/(str(orgs[0])+".json");receipt.write_text(json.dumps({"org_ids":[str(item) for item in orgs]}),encoding="utf-8")
        db.commit()
    try:yield SimpleNamespace(engine=test_engine,orgs=orgs)
    finally:
        with Session(test_engine) as db:
            for oid in orgs:
                org=db.get(Organization,oid);assert org and org.code.startswith("s13-seed-")
            db.execute(delete(AdminTemplate).where(AdminTemplate.organization_id.in_(orgs)))
            db.execute(delete(AdminChannel).where(AdminChannel.organization_id.in_(orgs)))
            db.execute(delete(Organization).where(Organization.id.in_(orgs)));db.commit()
            assert db.scalar(select(func.count()).select_from(Organization).where(Organization.id.in_(orgs)))==0
        receipt.unlink()


def seed(c, kind, writer, index=0, lock_timeout_ms=None):
    try:
        with Session(c.engine) as db:
            info=db.connection().info;info["s13_writer"]=writer
            if lock_timeout_ms is not None:
                db.execute(text("SELECT set_config('lock_timeout', :timeout, true)"),{"timeout":str(lock_timeout_ms)+"ms"})
            try:
                organization=db.get(Organization,c.orgs[index])
                getattr(admin,"_ensure_default_admin_"+kind)(db,organization);db.commit()
            finally:info.pop("s13_writer",None)
        return "ok"
    except Exception as error:return type(error).__name__


@pytest.mark.parametrize("first_kind,second_kind",[("channels","templates"),("templates","templates")])
def test_cold_parallel_seed_keeps_one_default_row(seed_context,first_kind,second_kind):
    c=seed_context;first_read=Event();second_read=Event();release=Event()
    def observe(conn,cursor,statement,parameters,context,executemany):
        sql=statement.lower()
        if not sql.lstrip().startswith("select") or "from admin_channels" not in sql:return
        writer=conn.info.get("s13_writer")
        if writer=="first" and not first_read.is_set():
            first_read.set();assert release.wait(8),"소유 시험의 첫 조회 해제 실패"
        elif writer=="second":second_read.set()
    event.listen(c.engine,"after_cursor_execute",observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            one=pool.submit(seed,c,first_kind,"first");assert first_read.wait(8)
            two=pool.submit(seed,c,second_kind,"second")
            # 원본에서는 두 조회가 모두 빈 결과를 보도록 재현한다. 잠금 보완 후에는 둘째가 조직 row에서 기다린다.
            second_read.wait(.5);release.set()
            results=[one.result(timeout=20),two.result(timeout=20)]
        assert results==["ok","ok"],results
        with Session(c.engine) as db:
            channels=list(db.scalars(select(AdminChannel).where(AdminChannel.organization_id==c.orgs[0])))
            templates=list(db.scalars(select(AdminTemplate).where(AdminTemplate.organization_id==c.orgs[0])))
            assert {item.code for item in channels}=={item["code"] for item in admin.DEFAULT_ADMIN_CHANNELS}
            assert len(channels)==len({item.code for item in channels})
            assert len(templates)==len({(item["channel_code"],item["name"]) for item in admin.DEFAULT_ADMIN_TEMPLATES})
    finally:release.set();event.remove(c.engine,"after_cursor_execute",observe)


def test_repeat_seed_preserves_existing_names_and_configuration(seed_context):
    c=seed_context;assert seed(c,"templates","first")=="ok"
    with Session(c.engine) as db:
        item=db.scalar(select(AdminChannel).where(AdminChannel.organization_id==c.orgs[0]).order_by(AdminChannel.code).limit(1));item.name="제작자 채널 이름";item.data_json={**item.data_json,"endpoint_url":"https://example.com/keep","s13_kept":True};cid=item.id;db.commit()
    assert seed(c,"templates","first")==seed(c,"channels","second")=="ok"
    with Session(c.engine) as db:
        item=db.get(AdminChannel,cid);assert item.name=="제작자 채널 이름" and item.data_json["endpoint_url"]=="https://example.com/keep" and item.data_json["s13_kept"] is True
        assert db.scalar(select(func.count()).select_from(AdminTemplate).where(AdminTemplate.organization_id==c.orgs[0]))==len(admin.DEFAULT_ADMIN_TEMPLATES)


def _seed_with_other_org_locked(c, target_index=1, wait_seconds=8, outcomes=None, lock_timeout_ms=None):
    def run():
        result=seed(c,"templates","other",target_index,lock_timeout_ms)
        if outcomes is not None:outcomes.append(result)
        return result
    with Session(c.engine) as held:
        held.scalar(select(Organization).where(Organization.id==c.orgs[0]).with_for_update())
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                assert pool.submit(run).result(timeout=wait_seconds)=="ok"
            finally:held.rollback()


def test_seed_for_another_organization_is_not_blocked(seed_context):
    _seed_with_other_org_locked(seed_context)


def test_failed_isolation_probe_releases_own_lock_before_joining_worker(seed_context):
    # 회귀를 의도적으로 같은 조직으로 유도한다. SQL timeout은 RED에서도 소유 worker가 스스로 종료하도록 제한한다.
    outcomes=[]
    with pytest.raises(TimeoutError):
        _seed_with_other_org_locked(seed_context,target_index=0,wait_seconds=.05,outcomes=outcomes,lock_timeout_ms=1000)
    assert outcomes==["ok"],"자기 잠금을 풀기 전에 worker 종료를 기다렸습니다."

"""S04: 제작자 모듈 결과와 기존 변수/Talk/미인식 경계. 실제 모델 호출 없음."""
from copy import deepcopy
from importlib import import_module,util
import pytest
from app.services.runtime_variables import get_variable,set_variable

CONFIG={"module_id":"secondary-module","outcome":{"variableName":"$apiResult.phase","answeredValue":"ok","unrecognizedValue":"not-found"}}

def subject():
    assert util.find_spec("app.services.secondary_llm") is not None, "S04 공통 결과/모듈 실행 helper가 필요합니다"
    return import_module("app.services.secondary_llm")

@pytest.mark.parametrize("value,status",[("ok","answered"),("not-found","unrecognized"),("timeout","error"),("","error"),(None,"error")])
def test_explicit_result_mapping_never_guesses_from_answer_text(value,status):
    state={"variables":{"$apiResult":{"phase":value}},"waitingNodeId":""}
    result=subject().evaluate_secondary_outcome(CONFIG,state,[{"type":"text","text":"텍스트 존재와 판정은 별개"}])
    assert result.status==status
    assert result.state is state


def test_pending_talk_is_not_dialog_end():
    state={"variables":{"$apiResult":{"phase":"ok"}},"waitingNodeId":"followup","dialogEnded":False}
    result=subject().evaluate_secondary_outcome(CONFIG,state,[{"type":"text","text":"추가 질문"}])
    assert result.status=="pending"


def test_transport_or_runtime_error_is_never_normal_unrecognized():
    state={"variables":{"$apiResult":{"phase":"not-found"}}}
    result=subject().evaluate_secondary_outcome(CONFIG,state,[],execution_error=True)
    assert result.status=="error"


def test_stale_outcome_is_cleared_without_losing_question_or_other_variables():
    state={"variables":{"$input":"원래 질문","$kept":"보존","$apiResult":{"phase":"ok","other":"보존할 응답"},"$apiResult.phase":"ok"}}
    calls=[]
    def run_module(document,next_state,module_id,defaults,channel):
        calls.append(module_id)
        assert get_variable(next_state["variables"],"$input")=="원래 질문"
        assert get_variable(next_state["variables"],"$kept")=="보존"
        assert get_variable(next_state["variables"],"$apiResult.other")=="보존할 응답"
        assert get_variable(next_state["variables"],"$apiResult.phase")==""
        next_state["dialogEnded"]=True
        return [],next_state,True
    result=subject().run_secondary_module({},state,CONFIG,run_module=run_module,default_messages={},channel_code="WEBCHAT")
    assert result.status=="error" and calls==["secondary-module"]


def test_local_miss_runs_creator_module_once_and_keeps_context():
    state={"variables":{"$input":"외부 질문","$custom":"기존 변수"}}
    calls=[]
    def run_module(document,next_state,module_id,defaults,channel):
        calls.append((module_id,channel))
        set_variable(next_state["variables"],"apiResult",{"phase":"ok"})
        next_state["dialogEnded"]=True
        return [{"type":"text","text":"기존 Talk 답변"}],next_state,True
    result=subject().run_secondary_module({},state,CONFIG,run_module=run_module,default_messages={},channel_code="WEBCHAT")
    assert result.status=="answered" and result.outputs[0]["text"]=="기존 Talk 답변"
    assert calls==[("secondary-module","WEBCHAT")]
    assert get_variable(result.state["variables"],"custom")=="기존 변수"
    assert not result.state.get("__secondaryLlm")


def test_secondary_cannot_reenter_itself():
    state={"variables":{}}
    nested=[]
    def run_module(document,next_state,module_id,defaults,channel):
        nested.append(subject().run_secondary_module(document,next_state,CONFIG,run_module=run_module,default_messages=defaults,channel_code=channel).status)
        set_variable(next_state["variables"],"apiResult",{"phase":"ok"})
        next_state["dialogEnded"]=True
        return [{"type":"text","text":"한번만 실행"}],next_state,True
    result=subject().run_secondary_module({},state,CONFIG,run_module=run_module,default_messages={},channel_code="WEBCHAT")
    assert nested==["error"] and result.status=="answered"


def test_pending_resume_does_not_execute_paid_module_again():
    calls=[]
    state={"variables":{"$input":"최초 질문"}}
    def run_module(document,next_state,module_id,defaults,channel):
        calls.append(module_id)
        next_state["waitingNodeId"]="followup"
        return [{"type":"text","text":"확인 질문"}],next_state,True
    result=subject().run_secondary_module({},state,CONFIG,run_module=run_module,default_messages={},channel_code="WEBCHAT")
    assert result.status=="pending" and result.state.get("__secondaryLlm")
    restored=deepcopy(result.state)
    restored["waitingNodeId"]=""
    restored["dialogEnded"]=True
    set_variable(restored["variables"],"apiResult",{"phase":"not-found"})
    resumed=subject().resume_secondary_outcome(restored,[])
    assert resumed.status=="unrecognized" and calls==["secondary-module"]
    assert not resumed.state.get("__secondaryLlm")

@pytest.mark.parametrize("outcome",[None,{}, {"variableName":"$r","answeredValue":"same","unrecognizedValue":"same"}])
def test_incomplete_mapping_refuses_execution_before_external_call(outcome):
    calls=[]
    config={**CONFIG,"outcome":outcome}
    result=subject().run_secondary_module({}, {"variables":{}},config,run_module=lambda *args:calls.append(True),default_messages={},channel_code="WEBCHAT")
    assert result.status=="error" and calls==[]


from tests.cga_alone.test_secondary_llm_config import secondary_bot

def test_creator_outcome_mapping_round_trips_existing_version_ai(secondary_bot):
    from app.api.routes import bots
    from app.schemas.bot import BotUpdateRequest
    from tests.integration.test_bot_persistence_regression import _request
    bot,version,_,user,db=secondary_bot
    bots.update_bot(bot.id,BotUpdateRequest(settings_scope=str(version.id),secondary_llm=CONFIG),_request(),current_user=user,db=db)
    db.refresh(version)
    assert bots._get_version_ai_config(bot,version)["secondary_llm"] == CONFIG


def test_activation_requires_explicit_creator_outcome_mapping(secondary_bot):
    from fastapi import HTTPException
    from app.api.routes import bots
    from tests.integration.test_bot_persistence_regression import _request
    bot,version,document,user,db=secondary_bot
    document["system_config"]["ai_config"]["secondary_llm"]={"module_id":"module-1"}
    bots._assign_version_document(version,document)
    db.flush()
    with pytest.raises(HTTPException) as error:
        bots.activate_version(bot.id,version.id,_request(),current_user=user,db=db)
    assert error.value.status_code==400 and "판정" in str(error.value.detail)


@pytest.fixture
def native_secondary_document():
    config=deepcopy(CONFIG)
    document={"dialogs":[{"id":"secondary-module","name":"제작자 LLM 모듈","dialogType":0},{"id":"local","name":"로컬 Q/A","dialogType":1}],
        "apis":[{"id":"answer-api","name":"제작자 답변 API","baseUrl":"http://fixture.invalid","methods":[{"id":"answer-method","name":"답변","httpMethod":"GET","methodUrl":"/answer","parameters":[{"id":"question","name":"q","location":"query"}]}]}],
        "dialog_flow_graphs":[{"id":"secondary-graph","dialogId":"secondary-module","name":"제작자 LLM 모듈","nodes":[
            {"id":"start","kind":"start","config":{}},
            {"id":"function","kind":"function","config":{"apiId":"answer-api","methodId":"answer-method","resultVariableName":"apiResult","outputMappings":[{"variableName":"$apiResult","path":"root.body"}],"parameterMappings":[{"parameterId":"question","value":"{{$input}}"}]}},
            {"id":"talk","kind":"talk","config":{"messageType":"text","basicMessages":["답변: {{$apiResult.answer}}"],"responseType":"none"}},
            {"id":"end","kind":"end","config":{}}],
            "links":[{"sourceNodeId":"start","sourcePort":"next","targetNodeId":"function"},{"sourceNodeId":"function","sourcePort":"next","targetNodeId":"talk"},{"sourceNodeId":"talk","sourcePort":"next","targetNodeId":"end"}]},
            {"id":"local-graph","dialogId":"local","name":"로컬 Q/A","nodes":[{"id":"local-start","kind":"start","config":{}},{"id":"local-talk","kind":"talk","config":{"messageType":"text","basicMessages":["정의한 로컬 답변"],"responseType":"none"}},{"id":"local-end","kind":"end","config":{}}],"links":[{"sourceNodeId":"local-start","sourcePort":"next","targetNodeId":"local-talk"},{"sourceNodeId":"local-talk","sourcePort":"next","targetNodeId":"local-end"}]}],
        "system_config":{"ai_config":{"nlu_type":"ml","nlu_model":"deep_learning_lite","answer_mode":"fixed","secondary_llm":config}}}
    return document


def primary_miss_handler():
    from app.api.routes import channels
    handler=getattr(channels,"_handle_local_intent_miss",None)
    assert callable(handler), "local miss와 최종 fallback 사이의 2차 실행 연결이 필요합니다"
    return handler


def fake_answer_http(monkeypatch, *, phase="ok", malformed=False, failure=False):
    from app.api.routes import channels
    import json
    calls=[]
    class Response:
        status=200;reason="OK"
        def __enter__(self):return self
        def __exit__(self,*args):return None
        def read(self):return b"invalid-response" if malformed else json.dumps({"phase":phase,"answer":"외부 생성 답변"}).encode()
    def send(request,timeout):
        calls.append(request.full_url)
        if failure:raise TimeoutError("synthetic API timeout")
        return Response()
    monkeypatch.setattr(channels,"urlopen",send)
    return calls

@pytest.mark.parametrize("phase",["ok","not-found","unexpected"])
def test_existing_function_and_talk_feed_explicit_secondary_result(native_secondary_document,monkeypatch,phase):
    from types import SimpleNamespace
    calls=fake_answer_http(monkeypatch,phase=phase)
    bot=SimpleNamespace(id="bot",data_json={})
    version=SimpleNamespace(id="version",name="v1",version_json=native_secondary_document)
    state={"variables":{"$input":"원래 질문","$custom":"보존"}}
    result=primary_miss_handler()(native_secondary_document,state,bot,version,{"system_error":"실행 오류"},"WEBCHAT")
    outputs,state=result.outputs,result.state
    assert result.status==({"ok":"answered","not-found":"unrecognized"}.get(phase,"error")) and len(calls)==1
    from urllib.parse import parse_qs,urlparse
    assert parse_qs(urlparse(calls[0]).query)["q"]==["원래 질문"]
    if phase=="ok":
        assert outputs[0]["text"]=="답변: 외부 생성 답변"
        assert state.get("intentFallbackCount",0)==0
    elif phase=="not-found":
        assert outputs==[] and state["intentFallbackCount"]==1
    else:
        assert outputs[0]["text"]=="실행 오류"
        assert state.get("intentFallbackCount",0)==0
    assert get_variable(state["variables"],"custom")=="보존"

@pytest.mark.parametrize("kind",["timeout","malformed"])
def test_secondary_api_failure_never_enters_recognition_fallback(native_secondary_document,monkeypatch,kind):
    from types import SimpleNamespace
    calls=fake_answer_http(monkeypatch,failure=kind=="timeout",malformed=kind=="malformed")
    bot=SimpleNamespace(id="bot",data_json={})
    version=SimpleNamespace(id="version",name="v1",version_json=native_secondary_document)
    result=primary_miss_handler()(native_secondary_document,{"variables":{"$input":"질문"}},bot,version,{"system_error":"실행 오류"},"WEBCHAT")
    outputs,state=result.outputs,result.state
    assert result.status=="error" and len(calls)==1 and outputs[0]["text"]=="실행 오류"
    assert state.get("intentFallbackCount",0)==0


def test_secondary_stable_id_cannot_be_shadowed_by_another_module_name(native_secondary_document,monkeypatch):
    from types import SimpleNamespace
    calls=fake_answer_http(monkeypatch)
    native_secondary_document["dialogs"].insert(0,{"id":"shadow","name":"secondary-module","dialogType":"0"})
    bot=SimpleNamespace(id="bot",data_json={})
    version=SimpleNamespace(id="version",name="v1",version_json=native_secondary_document)
    result=primary_miss_handler()(native_secondary_document,{"variables":{"$input":"질문"}},bot,version,{"system_error":"실행 오류"},"WEBCHAT")
    assert result.status=="answered" and len(calls)==1 and result.outputs[0]["text"]=="답변: 외부 생성 답변"


def test_final_overflow_cannot_reenter_the_secondary_module(native_secondary_document,monkeypatch):
    from types import SimpleNamespace
    calls=fake_answer_http(monkeypatch,phase="not-found")
    bot=SimpleNamespace(id="bot",data_json={"settings_by_version":{"v1":{"conversationDefaults":{"intentDetection":{"retryCount":1,"overflowModule":"제작자 LLM 모듈"}}}}})
    version=SimpleNamespace(id="version",name="v1",version_json=native_secondary_document)
    result=primary_miss_handler()(native_secondary_document,{"variables":{"$input":"질문"}},bot,version,{},"WEBCHAT")
    assert result.status=="unrecognized" and calls and len(calls)==1 and result.outputs==[]

@pytest.mark.parametrize("hit",[True,False])
@pytest.mark.parametrize("queued",[False,True])
def test_native_channel_hit_skips_secondary_and_miss_executes_once(secondary_bot,native_secondary_document,monkeypatch,hit,queued):
    from contextlib import contextmanager
    from uuid import UUID
    from app.api.routes import bots,channels
    from app.core.config import settings
    from app.models import AdminChannel,ChannelQueueEvent,ChannelMessage
    from app.schemas.bot import VersionUpdateRequest
    from tests.integration.test_bot_persistence_regression import _request
    from sqlalchemy import select
    bot,version,_,user,db=secondary_bot
    bots.update_version(bot.id,version.id,VersionUpdateRequest(version_json=native_secondary_document),_request(),current_user=user,db=db)
    bots.activate_version(bot.id,version.id,_request(),current_user=user,db=db)
    db.add(AdminChannel(organization_id=bot.organization_id,code="WEBCHAT",name="QA",status="active"));db.flush()
    @contextmanager
    def session():yield db
    monkeypatch.setattr(channels,"SessionLocal",session)
    monkeypatch.setattr(settings,"webchat_api_key","s04-qa-key")
    local=native_secondary_document["dialogs"][1]
    monkeypatch.setattr(channels,"_select_dialog_for_bot",lambda *args,**kwargs:(local,1.0) if hit else (None,0.0))
    calls=fake_answer_http(monkeypatch)
    created=channels.create_channel_room("webchat",channels.ChannelRoomCreateRequest(bot_id=str(bot.id),start_immediately=False),_request(),"s04-qa-key")["data"]
    room_id=UUID(created["room"]["id"])
    sent=channels.create_channel_room_message("webchat",room_id,channels.ChannelMessageRequest(message="현재 질문",defer_processing=queued),_request(),"s04-qa-key")["data"]
    if queued:
        assert sent["queued"] is True
        event=db.get(ChannelQueueEvent,UUID(sent["queueEvent"]["id"]))
        channels._process_channel_queue_event(db,_request(),"webchat",event)
        db.flush()
    answers=list(db.scalars(select(ChannelMessage).where(ChannelMessage.room_id==room_id,ChannelMessage.participant_kind=="bot")))
    assert len(calls)==(0 if hit else 1), {"calls":calls,"answers":[item.text for item in answers]}
    assert answers[-1].text==("정의한 로컬 답변" if hit else "답변: 외부 생성 답변")
    if not hit:
        from urllib.parse import parse_qs, urlparse
        assert parse_qs(urlparse(calls[0]).query).get("q") == ["현재 질문"]


def test_secondary_is_not_implicitly_started_as_first_graph(native_secondary_document):
    from types import SimpleNamespace
    from app.api.routes import channels
    bot=SimpleNamespace(id="bot",data_json={})
    version=SimpleNamespace(id="version",name="v1",version_json=native_secondary_document)
    state=channels._initial_runtime_state_for_version(bot,version)
    assert not state.get("graphId"), "구성된 2차 모듈은 첫 graph라는 이유로 1차 분류 전에 실행하면 안 됩니다"


@pytest.mark.parametrize("phase",["ok","not-found"])
def test_native_pending_talk_resumes_existing_module_without_reentry(native_secondary_document,monkeypatch,phase):
    from types import SimpleNamespace
    from app.api.routes import channels
    calls=fake_answer_http(monkeypatch,phase=phase)
    graph=native_secondary_document["dialog_flow_graphs"][0]
    graph["nodes"].insert(1,{"id":"followup","kind":"talk","config":{"messageType":"text","basicMessages":["추가 설명을 입력해주세요"],"responseType":"relay", "responseVariableName":"followup"}})
    graph["links"][0]["targetNodeId"]="followup"
    graph["links"].append({"sourceNodeId":"followup","sourcePort":"next","targetNodeId":"function"})
    bot=SimpleNamespace(id="bot",data_json={})
    version=SimpleNamespace(id="version",name="v1",version_json=native_secondary_document)
    first=primary_miss_handler()(native_secondary_document,{"variables":{"$input":"최초 질문"}},bot,version,{"system_error":"실행 오류"},"WEBCHAT")
    assert first.status=="pending" and first.state["waitingNodeId"]=="followup" and calls==[]
    outputs,state=channels._handle_runtime_message(native_secondary_document,first.state,"추가 설명",{},"WEBCHAT")
    finish=getattr(channels,"_finish_secondary_runtime",None)
    assert callable(finish), "기존 Talk 재개의 결과 판정 연결이 필요합니다"
    second=finish(native_secondary_document,state,bot,version,outputs,{"system_error":"실행 오류"},"WEBCHAT")
    assert len(calls)==1 and second.status==("answered" if phase=="ok" else "unrecognized")
    assert not second.state.get("__secondaryLlm")


@pytest.mark.parametrize("configured", [False, True])
def test_semantic_transport_failure_is_not_secondary_recognition_miss(native_secondary_document, monkeypatch, configured):
    from types import SimpleNamespace
    from app.api.routes import channels
    from app.services.vector_search import VectorSearchError
    native_secondary_document["system_config"]["ai_config"]["nlu_type"] = "semantic"
    if not configured:
        native_secondary_document["system_config"]["ai_config"].pop("secondary_llm")
    bot = SimpleNamespace(id="bot", data_json={})
    version = SimpleNamespace(id="version", name="v1", version_json=native_secondary_document)
    def fail(*args): raise VectorSearchError("synthetic semantic unavailable")
    monkeypatch.setattr(channels, "_semantic_select_dialog", fail)
    if configured:
        with pytest.raises(VectorSearchError):
            channels._select_dialog_for_bot(native_secondary_document, bot, version, "질문", prefer_exact_utterance=False)
    else:
        assert channels._select_dialog_for_bot(native_secondary_document, bot, version, "질문", prefer_exact_utterance=False) == (None, 0.0)


@pytest.mark.parametrize("pending", [False, True])
def test_legacy_webchat_runs_native_secondary_and_reuses_room(secondary_bot, native_secondary_document, monkeypatch, pending):
    from contextlib import contextmanager
    from app.api.routes import bots, channels, webchat
    from app.core.config import settings
    from app.models import AdminChannel, ChannelRoom, ChannelMessage
    from app.schemas.bot import VersionUpdateRequest
    from tests.integration.test_bot_persistence_regression import _request
    from sqlalchemy import select
    bot, version, _, user, db = secondary_bot
    if pending:
        graph = native_secondary_document["dialog_flow_graphs"][0]
        graph["nodes"].insert(1, {"id":"followup", "kind":"talk", "config":{"messageType":"text", "basicMessages":["추가 설명"], "responseType":"relay", "responseVariableName":"followup"}})
        graph["links"][0]["targetNodeId"] = "followup"
        graph["links"].append({"sourceNodeId":"followup", "sourcePort":"next", "targetNodeId":"function"})
    bots.update_version(bot.id, version.id, VersionUpdateRequest(version_json=native_secondary_document), _request(), current_user=user, db=db)
    bots.activate_version(bot.id, version.id, _request(), current_user=user, db=db)
    db.add(AdminChannel(organization_id=bot.organization_id, code="WEBCHAT", name="QA", status="active")); db.flush()
    @contextmanager
    def session(): yield db
    monkeypatch.setattr(channels, "SessionLocal", session)
    monkeypatch.setattr(webchat, "SessionLocal", session)
    monkeypatch.setattr(settings, "webchat_api_key", "s04-qa-key")
    monkeypatch.setattr(channels, "_select_dialog_for_bot", lambda *a, **kw: (None, 0.0))
    monkeypatch.setattr(webchat, "_select_dialog_for_bot", lambda *a, **kw: (None, 0.0))
    calls = fake_answer_http(monkeypatch)
    def send(text):
        return webchat.create_webchat_room_message(str(bot.id), "existing-client-room", webchat.WebchatMessageRequest(message=text), _request(), "s04-qa-key")["data"]
    first = send("질문")
    assert first["roomId"] == "existing-client-room"
    assert first["botMessage"]["text"] == ("추가 설명" if pending else "답변: 외부 생성 답변")
    if pending:
        assert calls == []
        second = send("후속 설명")
        assert second["botMessage"]["text"] == "답변: 외부 생성 답변"
    assert len(calls) == 1
    db.flush()
    rooms = list(db.scalars(select(ChannelRoom).where(ChannelRoom.bot_id == bot.id)))
    assert len(rooms) == 1
    history = list(db.scalars(select(ChannelMessage).where(ChannelMessage.room_id == rooms[0].id)))
    assert len(history) == (4 if pending else 2)


def test_existing_hub_initial_graph_is_preserved(native_secondary_document):
    from types import SimpleNamespace
    from app.api.routes import channels
    bot = SimpleNamespace(id="bot", data_json={"bot_kind":"hub"})
    version = SimpleNamespace(id="version", name="v1", version_json=native_secondary_document)
    assert channels._initial_runtime_state_for_version(bot, version)["graphId"] == "secondary-graph"


@pytest.mark.parametrize("phase", ["ok", "not-found", "unexpected"])
@pytest.mark.parametrize("pending", [False, True])
def test_secondary_session_end_judges_before_clearing_variables(native_secondary_document, monkeypatch, phase, pending):
    from types import SimpleNamespace
    from app.api.routes import channels
    calls=fake_answer_http(monkeypatch,phase=phase)
    graph=native_secondary_document["dialog_flow_graphs"][0]
    graph["nodes"][-1]["config"]["endSessionImmediately"]=True
    if pending:
        graph["nodes"].insert(1,{"id":"followup","kind":"talk","config":{"messageType":"text","basicMessages":["추가 설명"],"responseType":"relay","responseVariableName":"followup"}})
        graph["links"][0]["targetNodeId"]="followup"
        graph["links"].append({"sourceNodeId":"followup","sourcePort":"next","targetNodeId":"function"})
    bot=SimpleNamespace(id="bot",data_json={})
    version=SimpleNamespace(id="version",name="v1",version_json=native_secondary_document)
    result=channels._handle_local_intent_miss(native_secondary_document,{"variables":{"$input":"질문","$kept":"보존"}},bot,version,{"system_error":"실행 오류"},"WEBCHAT")
    if pending:
        assert result.status=="pending" and not calls
        outputs,state=channels._handle_runtime_message(native_secondary_document,result.state,"추가 설명",{},"WEBCHAT")
        result=channels._finish_secondary_runtime(native_secondary_document,state,bot,version,outputs,{"system_error":"실행 오류"},"WEBCHAT")
    assert result.status=={"ok":"answered","not-found":"unrecognized","unexpected":"error"}[phase]
    assert len(calls)==1 and result.state.get("sessionEnded") is True and result.state["variables"]=={}
    if phase=="ok":assert result.outputs[0]["text"]=="답변: 외부 생성 답변"
    if phase=="not-found":assert result.outputs==[]

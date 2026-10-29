"""기존 전용 PostgreSQL/rollback fixture를 그대로 사용한다."""
from tests.integration.conftest import db_session, test_engine
from tests.integration.test_bot_persistence_regression import migrated_test_database, regression_actor

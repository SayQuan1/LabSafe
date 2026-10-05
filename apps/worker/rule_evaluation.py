"""Optional q.general consumer for rule_evaluation tasks."""


def consume_rule(message):
    from packages.application.rule_execution import RuleExecution
    from packages.persistence.database import database_engine
    from packages.persistence.dispatch import validate_dispatch

    validate_dispatch(message)
    engine = database_engine()
    try:
        return RuleExecution(engine).execute(message)
    finally:
        engine.dispose()

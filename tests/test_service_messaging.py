"""MQTT presence and RabbitMQ, set up as in the admin portal."""


def test_messaging_is_off_locally_and_on_in_production(monkeypatch):
    """MQTT presence and RabbitMQ default on in production, off elsewhere; each can be switched explicitly."""
    import server

    monkeypatch.setenv("APP_ENV", "production")
    assert server._messaging_default() == "true"
    monkeypatch.setenv("APP_ENV", "development")
    assert server._messaging_default() == "false"


def test_presence_announces_marketing_on_its_own_topic():
    from services.mqtt_presence import MqttServicePresence

    presence = MqttServicePresence(service_name="marketing", instance_id="marketing-01")
    assert presence.status_topic == "services/marketing/marketing-01/status"


def test_each_worker_announces_itself_separately(monkeypatch):
    """With 2 workers, one restarting must not mark the whole service offline."""
    import os

    import server

    monkeypatch.setenv("SERVICE_INSTANCE_ID", "marketing-01")
    assert server._presence_instance_id() == f"marketing-01-{os.getpid()}"

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import AgeLimit, TimeLimit, Retries, Prometheus

from config.settings import settings

# Remove Prometheus to avoid port conflicts when running multiple worker processes
broker = RedisBroker(url=settings.redis_url, middleware=[])
broker.add_middleware(AgeLimit())
broker.add_middleware(TimeLimit())
broker.add_middleware(Retries(max_retries=3))
dramatiq.set_broker(broker)

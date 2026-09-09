# نقطة دخول تطبيق Python على استضافة Serv00 (Phusion Passenger = WSGI).
# يحوّل تطبيق FastAPI (ASGI) إلى WSGI عبر a2wsgi.
from a2wsgi import ASGIMiddleware

from server import app

application = ASGIMiddleware(app)
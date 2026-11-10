"""Copy into the WSGI configuration file linked from PythonAnywhere's Web tab."""
import os
import sys

project_path = "/home/YOUR_USERNAME/PeerTrain"
if project_path not in sys.path:
    sys.path.insert(0, project_path)

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()

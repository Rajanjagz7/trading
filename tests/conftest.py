import os
import sys

# Allow `from api.nse_service import ...` when pytest is run from the repo
# root without the project being pip-installed.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

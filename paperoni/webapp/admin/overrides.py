import gifnoc
import yaml

from ...config import papconf
from ..common import FileEditor, mila_template


class ConfigFile:
    def __init__(self, file):
        self.file = file

    def read(self):
        return self.file.read_text()

    def write(self, new_permissions, dry=False):
        d = yaml.safe_load(new_permissions)
        with gifnoc.overlay(d):
            pass
        if not dry:
            self.file.write_text(new_permissions)
            gifnoc.current_configuration().refresh()


@mila_template(help="/help#overrides")
async def app(page, box):
    """Update overrides."""
    await FileEditor(
        ConfigFile(papconf.paths.database.parent / "overrides.yaml")
    ).run(box)


ROUTES = app

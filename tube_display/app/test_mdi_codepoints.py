"""The icon map is built from the official stylesheet at image build time; a parse miss means blank icons."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("mdi_codepoints", Path(__file__).parent / "tools" / "mdi_codepoints.py")
mdi_codepoints = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mdi_codepoints)

# The format @mdi/font 7.x ships: double-colon ::before, one declaration per line
CSS_7X = '''.mdi-ab-testing::before {
  content: "\\F01C9";
}

.mdi-account::before {
  content: "\\F0004";
}
'''

# Older releases used a single colon
CSS_OLD = '.mdi-robot:before { content: "\\F06A9"; }'


def test_parses_double_colon_rules(tmp_path):
    p = tmp_path / "mdi.css"
    p.write_text(CSS_7X)
    assert mdi_codepoints.build(str(p)) == {"ab-testing": 0xF01C9, "account": 0xF0004}


def test_parses_single_colon_rules(tmp_path):
    p = tmp_path / "mdi.css"
    p.write_text(CSS_OLD)
    assert mdi_codepoints.build(str(p)) == {"robot": 0xF06A9}

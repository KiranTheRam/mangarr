from mangarr import USER_AGENT, __version__


def test_user_agent_names_version_and_project_repository():
    assert USER_AGENT == f"Mangarr/{__version__} (https://github.com/KiranTheRam/mangarr)"

from setuptools import find_packages, setup

setup(
    name="dispatch-client",
    version="0.3.0",
    description="wuzhu-dispatch client CLI, Config Skill & Runtime Skill",
    packages=find_packages(),
    install_requires=[
        "click>=8.1.0",
        "requests>=2.31.0",
        "pyyaml>=6.0",
    ],
    entry_points={
        "console_scripts": [
            "dispatch-client=dispatch_client.main:cli",
            "dispatch-config=dispatch_client.cli_config:config_cli",
            "dispatch-skill=dispatch_client.cli_skill:skill_cli",
        ],
    },
    python_requires=">=3.11",
)

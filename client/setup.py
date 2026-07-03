from setuptools import find_packages, setup

setup(
    name="dispatch-client",
    version="0.2.0",
    description="wuzhu-dispatch client CLI",
    packages=find_packages(),
    install_requires=[
        "click>=8.1.0",
        "requests>=2.31.0",
        "pyyaml>=6.0",
    ],
    entry_points={
        "console_scripts": [
            "dispatch-client=dispatch_client.main:cli",
        ],
    },
    python_requires=">=3.11",
)

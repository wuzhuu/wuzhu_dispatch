from setuptools import find_packages, setup

setup(
    name="dispatch-compute-server",
    version="0.2.0",
    description="wuzhu-dispatch compute server (worker daemon)",
    packages=find_packages(),
    install_requires=[
        "requests>=2.31.0",
        "psutil>=5.9.0",
        "pyyaml>=6.0",
    ],
    entry_points={
        "console_scripts": [
            "dispatch-compute-server=dispatch_compute_server.main:main",
        ],
    },
    python_requires=">=3.11",
)

import subprocess
try:
    stdout = subprocess.check_output(
        ["hwcomponents_cacti/destiny_3d_cache/destiny", "test.cfg"],
        stderr=subprocess.STDOUT
    ).decode("utf-8")
    print(stdout[:500])
except subprocess.CalledProcessError as e:
    print(f"Error: {e.output.decode('utf-8')}")

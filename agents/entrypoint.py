"""
Container entrypoint. Selects which agent to serve based on AGENT_KIND so a
single image backs both AgentCore runtimes (intake, assessment).
"""

import os

kind = os.environ.get("AGENT_KIND", "assessment").lower()

if kind == "intake":
    from intake.agent import app
else:
    from assessment.agent import app

if __name__ == "__main__":
    app.run()

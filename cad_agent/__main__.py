import os
import uvicorn
from cad_agent.config import local_env

if __name__ == "__main__":
    env = local_env()
    uvicorn.run(
        "cad_agent.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=int(env.get("CAD_AGENT_PORT", "8765")),
    )

\# Rules for AI coding tools on this repo

\- Read contracts.md and TEAM.md before writing code. Follow the JSON shapes exactly.

\- Only edit files in YOUR folder (see TEAM.md for your branch -> folder). Never edit another person's folder.

\- Keep the existing function signatures in the stubs; other modules call them.

\- Robot Shop lives in ../robot-shop and runs as compose project "robot-shop". Never modify its files.

\- Never target containers outside project robot-shop. Every fault must return a revert function.

\- Never invent metrics. Observations and evidence come only from measured data.

\- Before finishing, run `python main.py --mock` and make sure it still works.

\- Python 3, no secrets in code; API keys go in .env (gitignored).


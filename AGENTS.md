# This repository

This branch is the finance policy and policy-backed funding edition of the local SME workflow.

- Read `INSTALL.md` and `skills/sme-finance-opportunities/SKILL.md` for this branch. Keep the general skill intact for compatibility.
- Finance scope covers tax relief, subsidies, employment/stabilization support and policy-backed interest subsidies/credit support. Qualification recognition itself and commercial loan product monitoring are outside the daily default scope.
- Preserve the user's daily log structure, real human financial review and realization evidence. Historical references and machine suggestions are not confirmed eligibility or received funds.
- Never commit real profiles, certificates, collected evidence, databases, exports, cookies, credentials, notification receipts, scheduler IDs, or chat IDs. Runtime workspaces belong outside this source checkout.
- `monitor.py` and `human_review.py` process collected JSON; they are not network collectors. Only call a run a new check after actually reading sources during that run.
- Human screening review and participation decisions are separate. Preserve real human decisions on update; never mark a human review complete from machine analysis or page opening.
- Installation prepares local files. Only the recipient's Codex automation tool can register tasks for the recipient's current chat. Do not create tasks merely while testing this repository.
- Before publishing, run `python -B -m unittest discover -s runtime/tests -v`, `python -B -m unittest discover -s tests -v`, and `python tools/check_package.py`. Tests use temporary directories only.

Sample CatchUpAI configuration templates, copied unchanged from `../CatchUpAI/configuration_templates/` at
CatchUpAI commit `d547231`, for the importer tests (`app/import_template.py`).
`all_section_types.json` is made up for the tests: it uses all five CatchUp section types.
These are kept outside `tests/fixtures/` on purpose: the job interview pack is generated from one of them, so it
shares their wording, and the overlap check only compares packs with `tests/fixtures/`.

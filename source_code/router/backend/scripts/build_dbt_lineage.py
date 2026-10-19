"""Build data/dbt_lineage.json, the model list and relationships used by
Settings > Run dbt to work out which upstream models a model needs.

Reads the manifest.json that dbt writes to target/ in each project under
C:\\Git (uk_dbt_raw_data, uk_dbt_transform_data, uk_dbt_product_data). The
product project pulls in transform and raw as packages, so its manifest alone
covers all three; the other two only add models it does not have.

Run after `dbt parse` (or any dbt run/compile) in uk_dbt_product_data, so the
manifests match the models on main, then deploy the site:

    python scripts/build_dbt_lineage.py
"""
import json
import os
import sys

GIT_ROOT = os.environ.get('DBT_GIT_ROOT', r'C:\Git')
PROJECTS = {
    'uk_dbt_raw_data':       'raw',
    'uk_dbt_transform_data': 'transform',
    'uk_dbt_product_data':   'product',
}
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'dbt_lineage.json')


def main():
    models, generated = {}, {}
    # Product first: its copy of each model wins.
    for repo in ('uk_dbt_product_data', 'uk_dbt_transform_data', 'uk_dbt_raw_data'):
        path = os.path.join(GIT_ROOT, repo, 'source_code', 'source', 'target', 'manifest.json')
        if not os.path.exists(path):
            print(f"Skipping {repo}: no {path} - run dbt parse there first")
            continue
        with open(path, encoding='utf-8') as f:
            manifest = json.load(f)
        generated[repo] = manifest['metadata'].get('generated_at')
        for node in manifest['nodes'].values():
            if node['resource_type'] != 'model' or node['package_name'] not in PROJECTS:
                continue
            if node['name'] in models:
                continue
            config = node.get('config') or {}
            models[node['name']] = {
                'project':      PROJECTS[node['package_name']],
                'materialized': config.get('materialized'),
                'schema':       node.get('schema'),
                'table':        node.get('alias') or node['name'],
                'tags':         node.get('tags') or [],
                'stage_url':    config.get('stage_url'),
                'parents':      sorted({p.split('.')[-1] for p in node['depends_on']['nodes']
                                        if p.startswith('model.') and p.split('.')[1] in PROJECTS}),
            }
    if not models:
        sys.exit("No models found - nothing written")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump({'generated_at': generated, 'models': dict(sorted(models.items()))}, f, indent=1)
    counts = {p: sum(1 for m in models.values() if m['project'] == p) for p in PROJECTS.values()}
    print(f"Wrote {len(models)} models to {OUT}: {counts}")


if __name__ == '__main__':
    main()

import json, sys
path = sys.argv[1]
keywords = sys.argv[2].split(',') if len(sys.argv) > 2 else []
with open(path) as f:
    nb = json.load(f)
for i, cell in enumerate(nb['cells']):
    src = ''.join(cell['source'])
    if keywords and not any(k in src for k in keywords):
        continue
    print("=== CELL %d (%s) ===" % (i, cell['cell_type']))
    print(src)
    print()

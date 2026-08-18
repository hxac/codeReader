import json

with open('decode.json') as f:
    data = json.load(f)
print('distributedInfo:', data['distributedInfo'])
print('traceEvents:', len(data['traceEvents']))

import json 
f = open('rank_history.json') 
store = json.load(f) 
f.close() 
keys = sorted(store.keys()) 
trimmed = {k: store[k] for k in keys[-2:]} 
f = open('rank_history.json', 'w') 
json.dump(trimmed, f, separators=(',', ':')) 
f.close() 
print('Kept', len(trimmed), 'days from', len(keys), 'total') 

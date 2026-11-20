import re,sys
for f in sys.argv[1:]:
    t=open(f+'.txt',encoding='utf8').read()
    t=re.sub(r'[ \t]+',' ',t); t=re.sub(r'\n\s*\n+','\n',t)
    # find question headers lines
    hits=[]
    for m in re.finditer(r'(?:^|\n)((?:שאלה|Question|QUESTION)[^\n]{0,3}\n?[^\n]{0,80})',t):
        hits.append(m.group(1).replace('\n',' ')[:90])
    pg=[(m.start(),m.group(1)) for m in re.finditer(r'=== page (\d+) ===',t)]
    print('#####',f,len(t)); print(' || '.join(hits[:16]))

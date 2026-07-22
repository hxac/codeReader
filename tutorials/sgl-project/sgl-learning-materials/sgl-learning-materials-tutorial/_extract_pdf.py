import sys
import pypdf

path = sys.argv[1]
r = pypdf.PdfReader(path)
print("PAGES:", len(r.pages))
for i, p in enumerate(r.pages):
    print("===PAGE %d===" % (i + 1))
    try:
        print(p.extract_text())
    except Exception as e:
        print("[extract error]", e)

"""Node 'numpy' completo: seções imports e imported by separadas."""
import re

html = open(r"build\minidm\xref-minidm.html", encoding="utf-8").read()
i = html.find('<a name="numpy"></a>')
nxt = html.find('<div class="node">', i)
seg = html[i:nxt]
a = seg.find("imports:")
b = seg.find("imported by:")
print("=== imports (numpy importa) — primeiros 10 ===")
imp = re.findall(r'href="#([^"]+)"', seg[a:b])
print(imp[:10], "... total", len(imp))
print("=== imported by (quem importa numpy) ===")
impby = re.findall(r'href="#([^"]+)"', seg[b:])
print(impby)

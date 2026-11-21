#!/usr/bin/env python3
"""さらとしなのイラスト（SVG）を描く。assets/ に全身と顔の2種類を書き出す。"""
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "assets"
INK, HAIR, SKIN = "#2B2620", "#1E1A19", "#FBEFE4"

def face(eyes):
    return f'''
  <ellipse cx="100" cy="100" rx="44" ry="42" fill="{SKIN}"/>
  <ellipse cx="84" cy="76" rx="6.5" ry="3.6" fill="#3A302C" opacity=".55"/>
  <ellipse cx="116" cy="76" rx="6.5" ry="3.6" fill="#3A302C" opacity=".55"/>
  {eyes}
  <ellipse cx="75" cy="113" rx="7.5" ry="4.2" fill="#F09AA6" opacity=".6"/>
  <ellipse cx="125" cy="113" rx="7.5" ry="4.2" fill="#F09AA6" opacity=".6"/>'''

SARA = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="VIEWBOX" role="img" aria-label="さら：十二単の姫君">
  <!-- 長い垂髪（後ろ） -->
  <path d="M54 92 C44 34 156 34 146 92 L166 250 Q100 262 34 250 Z" fill="{HAIR}"/>
  <!-- 十二単：内側の衣ほど裾と袖口がのぞく -->
  <path d="M54 150 Q100 136 146 150 L184 254 Q100 264 16 254 Z" fill="#8E354A"/>
  <path d="M57 150 Q100 138 143 150 L178 248 Q100 257 22 248 Z" fill="#D9798A"/>
  <path d="M60 150 Q100 140 140 150 L172 242 Q100 250 28 242 Z" fill="#EFC3C3"/>
  <path d="M63 150 Q100 142 137 150 L166 236 Q100 243 34 236 Z" fill="#FBF8F0"/>
  <path d="M66 150 Q100 144 134 150 L160 230 Q100 236 40 230 Z" fill="#D9798A"/>
  <!-- 唐衣（からぎぬ） -->
  <path d="M64 150 Q100 142 136 150 L150 196 Q100 204 50 196 Z" fill="#6E4F86"/>
  <path d="M64 150 Q100 142 136 150 L140 160 Q100 152 60 160 Z" fill="#8466A0"/>
  <!-- 襟元のかさね -->
  <path d="M84 146 L100 178 L116 146" fill="none" stroke="#8E354A" stroke-width="5"/>
  <path d="M87 146 L100 172 L113 146" fill="none" stroke="#EFC3C3" stroke-width="4"/>
  <path d="M90 146 L100 166 L110 146" fill="none" stroke="#FBF8F0" stroke-width="4"/>
  <!-- 広い袖 -->
  <path d="M60 156 Q34 170 30 228 L72 230 Q74 196 74 170 Z" fill="#D9798A"/>
  <path d="M30 228 L72 230 L72 236 L29 234 Z" fill="#8E354A"/>
  <path d="M31 222 L72 224 L72 230 L30 228 Z" fill="#EFC3C3"/>
  <path d="M140 156 Q166 170 170 228 L128 230 Q126 196 126 170 Z" fill="#D9798A"/>
  <path d="M170 228 L128 230 L128 236 L171 234 Z" fill="#8E354A"/>
  <path d="M169 222 L128 224 L128 230 L170 228 Z" fill="#EFC3C3"/>
  <!-- 短冊と手 -->
  <rect x="93" y="168" width="14" height="52" rx="2" fill="#E9E1F2" stroke="#B7A6C6" stroke-width="1"/>
  <path d="M100 176 L100 210" stroke="#6E4F86" stroke-width="1.4" stroke-dasharray="3 3"/>
  <ellipse cx="89" cy="200" rx="7" ry="6" fill="{SKIN}"/>
  <ellipse cx="111" cy="200" rx="7" ry="6" fill="{SKIN}"/>
  {face('<path d="M79 101 Q86 93 93 101" fill="none" stroke="' + INK + '" stroke-width="2.8" stroke-linecap="round"/><path d="M107 101 Q114 93 121 101" fill="none" stroke="' + INK + '" stroke-width="2.8" stroke-linecap="round"/><path d="M95 117 Q100 123 105 117 Z" fill="#C8505E"/>')}
  <!-- 前髪（中央で分けた額）と鬢削ぎ -->
  <path d="M54 102 C46 46 154 46 146 102 C142 74 124 60 100 58 C76 60 58 74 54 102 Z" fill="{HAIR}"/>
  <path d="M100 58 Q98 66 100 70 Q102 66 100 58 Z" fill="{HAIR}"/>
  <path d="M55 96 Q50 128 60 142 L67 139 Q60 120 62 98 Z" fill="{HAIR}"/>
  <path d="M145 96 Q150 128 140 142 L133 139 Q140 120 138 98 Z" fill="{HAIR}"/>
</svg>
'''

SHINA = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="VIEWBOX" role="img" aria-label="しな：烏帽子に狩衣の公達">
  <!-- 指貫（さしぬき）の袴 -->
  <path d="M50 206 L150 206 L170 248 Q150 258 130 250 Q100 258 70 250 Q50 258 30 248 Z" fill="#6E4F86"/>
  <path d="M100 210 L100 252" stroke="#58406C" stroke-width="2"/>
  <!-- 狩衣（かりぎぬ） -->
  <path d="M56 150 Q100 136 144 150 L158 220 Q100 230 42 220 Z" fill="#2F6E9C"/>
  <path d="M100 150 L100 224" stroke="#285F86" stroke-width="1.5"/>
  <!-- 丸い襟と下の白 -->
  <ellipse cx="100" cy="146" rx="30" ry="11" fill="#285F86"/>
  <ellipse cx="100" cy="144" rx="22" ry="7" fill="#FBF8F0"/>
  <!-- 袖と袖括りの紐 -->
  <path d="M60 156 Q32 172 30 222 L74 222 Q74 194 74 168 Z" fill="#2F6E9C"/>
  <path d="M140 156 Q168 172 170 222 L126 222 Q126 194 126 168 Z" fill="#2F6E9C"/>
  <path d="M30 222 L74 222" stroke="#9DBCD6" stroke-width="3"/>
  <path d="M126 222 L170 222" stroke="#9DBCD6" stroke-width="3"/>
  <circle cx="38" cy="226" r="3.2" fill="#D9798A"/><circle cx="66" cy="226" r="3.2" fill="#D9798A"/>
  <circle cx="134" cy="226" r="3.2" fill="#D9798A"/><circle cx="162" cy="226" r="3.2" fill="#D9798A"/>
  <!-- 笏（しゃく）と手 -->
  <rect x="95" y="160" width="10" height="56" rx="4" fill="#E9DCC3" stroke="#C8B48E" stroke-width="1"/>
  <ellipse cx="100" cy="198" rx="12" ry="7" fill="{SKIN}"/>
  {face('<circle cx="86" cy="100" r="5" fill="' + INK + '"/><circle cx="87.8" cy="98.2" r="1.7" fill="#fff"/><circle cx="114" cy="100" r="5" fill="' + INK + '"/><circle cx="115.8" cy="98.2" r="1.7" fill="#fff"/><path d="M94 116 Q100 121 106 116" fill="none" stroke="#B5525F" stroke-width="2.4" stroke-linecap="round"/>')}
  <!-- 髪と烏帽子（えぼし） -->
  <path d="M56 98 C50 58 150 58 144 98 C140 76 124 66 100 66 C76 66 60 76 56 98 Z" fill="{HAIR}"/>
  <path d="M74 70 C70 30 86 8 108 8 C126 10 132 34 128 70 Z" fill="{HAIR}"/>
  <path d="M86 34 Q100 26 120 32" fill="none" stroke="#3A3330" stroke-width="2"/>
  <path d="M80 54 Q102 46 126 52" fill="none" stroke="#3A3330" stroke-width="2"/>
  <path d="M74 70 Q100 62 128 70" fill="none" stroke="#3A3330" stroke-width="2.5"/>
</svg>
'''

for name, svg in (("sara", SARA), ("shina", SHINA)):
    (OUT / f"{name}.svg").write_text(svg.replace("VIEWBOX", "0 0 200 264"), encoding="utf-8")
    (OUT / f"{name}-face.svg").write_text(svg.replace("VIEWBOX", "48 40 104 104"), encoding="utf-8")
print("assets/ に書き出しました")

# ---- ラジオブースの場面（茶話ページの一番上に埋め込む） ----
import re as _re

def inner(svg):
    return _re.sub(r'^.*?<svg[^>]*>|</svg>\s*$', '', svg, flags=_re.S)

def phones(cx, cy, band=True, cups=True):
    out = ""
    if band:
        out += f'<path d="M{cx-54} {cy} C{cx-54} {cy-80} {cx+54} {cy-80} {cx+54} {cy}" fill="none" stroke="#3A2E28" stroke-width="7" stroke-linecap="round"/>'
    if cups:
        for x in (cx - 60, cx + 46):
            out += f'<rect x="{x}" y="{cy-14}" width="14" height="30" rx="7" fill="#3A2E28"/><rect x="{x+3}" y="{cy-8}" width="8" height="18" rx="4" fill="#D9798A"/>'
    return out

def mic(base_x, tip_x, tip_y):
    return (f'<ellipse cx="{base_x}" cy="344" rx="18" ry="5" fill="#3A2E28"/>'
            f'<path d="M{base_x} 342 L{tip_x} {tip_y+14}" stroke="#4A3D35" stroke-width="5" stroke-linecap="round"/>'
            f'<rect x="{tip_x-9}" y="{tip_y-14}" width="18" height="30" rx="9" fill="#5A4C43"/>'
            f'<path d="M{tip_x-7} {tip_y-6} H{tip_x+7} M{tip_x-7} {tip_y} H{tip_x+7} M{tip_x-7} {tip_y+6} H{tip_x+7}" stroke="#8A7A6E" stroke-width="1.5"/>')

S = 1.15
SX, SY = 105, 100   # さらの位置
HX, HY = 385, 100   # しなの位置
sara_head = (SX + 100 * S, SY + 100 * S)
shina_head = (HX + 100 * S, HY + 100 * S)

BOOTH = f'''<svg class="booth" viewBox="0 0 720 420" role="img" aria-label="ラジオブースでマイクに向かう、十二単のさらと烏帽子のしな" xmlns="http://www.w3.org/2000/svg">
  <rect width="720" height="420" fill="#EFE6D3"/>
  <!-- 障子 -->
  <g>
    <rect x="24" y="36" width="170" height="210" fill="#FBF8F0" stroke="#8B6B47" stroke-width="6"/>
    <path d="M24 106 H194 M24 176 H194 M81 36 V246 M138 36 V246" stroke="#8B6B47" stroke-width="2.5"/>
    <rect x="526" y="36" width="170" height="210" fill="#FBF8F0" stroke="#8B6B47" stroke-width="6"/>
    <path d="M526 106 H696 M526 176 H696 M583 36 V246 M640 36 V246" stroke="#8B6B47" stroke-width="2.5"/>
  </g>
  <!-- ON AIR の行灯 -->
  <path d="M360 0 V18" stroke="#3A2E28" stroke-width="2"/>
  <rect x="300" y="18" width="120" height="40" rx="8" fill="#3A2E28"/>
  <rect x="307" y="24" width="106" height="28" rx="5" fill="#C8505E"/>
  <text x="360" y="44" text-anchor="middle" font-family="Shippori Mincho, serif" font-weight="700" font-size="17" fill="#FFF3F0" letter-spacing="3">ON AIR</text>
  <!-- 番組名の木札 -->
  <rect x="268" y="70" width="184" height="40" rx="4" fill="#D6B48A" stroke="#A0764B" stroke-width="2"/>
  <text x="360" y="98" text-anchor="middle" font-family="Yuji Syuku, serif" font-size="21" fill="#2B2620">さらとしなの茶話</text>
  <!-- しなのヘッドホンの帯は烏帽子の後ろ側 -->
  {phones(*shina_head, band=True, cups=False)}
  <g transform="translate({SX} {SY}) scale({S})">{inner(SARA.replace("VIEWBOX", "0 0 200 264"))}</g>
  <g transform="translate({HX} {HY}) scale({S})">{inner(SHINA.replace("VIEWBOX", "0 0 200 264"))}</g>
  {phones(*sara_head, band=True, cups=True)}
  {phones(*shina_head, band=False, cups=True)}
  <!-- 机 -->
  <rect x="0" y="340" width="720" height="80" fill="#A0764B"/>
  <rect x="0" y="336" width="720" height="10" fill="#C49A6B"/>
  {mic(318, 292, 272)}
  {mic(402, 428, 272)}
  <!-- 湯呑と急須 -->
  <path d="M344 326 Q345 344 352 344 H368 Q375 344 376 326 Z" fill="#6E8B6A"/>
  <ellipse cx="360" cy="326" rx="16" ry="4" fill="#8FAA8A"/>
  <path d="M356 316 Q352 308 358 302 M364 316 Q368 308 362 300" fill="none" stroke="#B9AD9E" stroke-width="1.6" stroke-linecap="round"/>
  <!-- 台本の短冊 -->
  <rect x="120" y="364" width="70" height="18" rx="2" fill="#F6E3E3" transform="rotate(-6 155 373)"/>
  <rect x="530" y="364" width="70" height="18" rx="2" fill="#E0E9F1" transform="rotate(5 565 373)"/>
</svg>'''
(Path(__file__).resolve().parent.parent / "templates" / "_booth.svg").write_text(BOOTH, encoding="utf-8")
print("templates/_booth.svg に書き出しました")

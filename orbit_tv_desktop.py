
from __future__ import annotations
import json
import io
import math
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from urllib.parse import urlsplit
try:
    from PIL import Image
except ImportError:
    Image=None

try:
    import customtkinter as ctk
except ImportError:
    raise SystemExit("Falta customtkinter. Instale com: python -m pip install -r requirements.txt")

APP_NAME = "Orbit TV"
BASE_DIR = Path(__file__).resolve().parent
CATALOG_PATH = BASE_DIR / "catalogo.txt"
BG, PANEL, PANEL2, BORDER = "#080C15", "#111827", "#182338", "#293750"
TEXT, MUTED, ACCENT, CYAN = "#F4F7FF", "#9AAAC1", "#777BFF", "#45DEC9"
GREEN, RED, AMBER, GREY = "#35C98A", "#F06470", "#F5B84B", "#7D8AA0"
SUPPORTED_SCHEMES = {"http", "https", "rtsp", "rtmp", "udp", "file"}
CACHE_SECONDS = 300
LIVE_REFRESH_SECONDS = 90


def find_vlc() -> str | None:
    candidates = [os.environ.get("VLC_PATH", ""), r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                  r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
                  "/Applications/VLC.app/Contents/MacOS/VLC", "/usr/bin/vlc", "/usr/local/bin/vlc", shutil.which("vlc") or ""]
    return next((p for p in candidates if p and (os.path.isfile(p) or shutil.which(p))), None)


def parse_catalog(path: Path) -> tuple[list[dict], list[str]]:
    channels, warnings, current, source = [], [], None, None
    if not path.is_file():
        return [], [f"Ficheiro não encontrado: {path}"]
    def finish():
        nonlocal current
        if current:
            channels.append(current)
            current = None
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            for lineno, raw in enumerate(f, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" not in line:
                    warnings.append(f"Linha {lineno}: formato inválido, ignorada.")
                    continue
                key, value = (x.strip() for x in line.split(":", 1))
                key = key.casefold()
                if key == "canal":
                    finish()
                    current = {"name": value or f"Canal {len(channels)+1}", "category": "Outros", "logo": "", "sources": []}
                    source = None
                elif current is None:
                    warnings.append(f"Linha {lineno}: campo antes do primeiro canal.")
                elif key == "categoria":
                    current["category"] = value or "Outros"
                elif key == "logo":
                    current["logo"] = value
                elif key == "fonte":
                    source = {"url": value, "quality": "Automática", "name": "", "referer": "", "agent": ""}
                    current["sources"].append(source)
                elif key in ("qualidade", "nome", "referer", "agente") and source:
                    source[{"qualidade":"quality", "nome":"name", "referer":"referer", "agente":"agent"}[key]] = value
                elif key == "chave":
                    warnings.append(f"Linha {lineno}: chave DRM ignorada (não suportada).")
        finish()
    except (OSError, UnicodeError) as exc:
        return [], [f"Erro lendo catálogo: {exc}"]
    return dedupe_channels(channels), warnings


def channel_key(name: str) -> str:
    value=unicodedata.normalize("NFKD",name.casefold())
    value="".join(ch for ch in value if not unicodedata.combining(ch))
    value=re.sub(r"\b(4k|uhd|fhd|hd|sd|720p|1080p|2160p)\b"," ",value)
    return re.sub(r"[^a-z0-9]+","",value)


def dedupe_channels(channels: list[dict]) -> list[dict]:
    
    merged={}; out=[]
    for channel in channels:
        key=channel_key(channel.get("name", ""))
        if not key:key=f"unnamed-{len(out)}"
        if key not in merged:
            item={"name":channel.get("name","Canal"),"category":channel.get("category","Outros"),"logo":channel.get("logo",""),"sources":[]}
            merged[key]=item;out.append(item)
        target=merged[key]; seen={s.get("url","").strip() for s in target["sources"]}
        for source in channel.get("sources",[]):
            url=source.get("url","").strip()
            if url and url not in seen:
                target["sources"].append(source.copy());seen.add(url)
        if target.get("category")=="Outros" and channel.get("category"):target["category"]=channel["category"]
    return out


def parse_m3u(path: Path) -> tuple[list[dict],list[str]]:
   
    channels=[]; warnings=[]; meta=None; referrer=""; agent=""
    try:
        lines=path.read_text(encoding="utf-8-sig",errors="replace").splitlines()
    except OSError as exc:return [],[f"Erro lendo playlist: {exc}"]
    if any(line.strip().upper().startswith(("#EXT-X-TARGETDURATION","#EXT-X-MEDIA-SEQUENCE","#EXT-X-STREAM-INF")) for line in lines):
        return [],["O arquivo parece ser uma playlist de mídia HLS, não uma lista de canais M3U. Importe a playlist de canais ou use o URL HLS no catálogo."]
    for line_no,raw in enumerate(lines,1):
        line=raw.strip()
        if not line or line=="#EXTM3U":continue
        if line.startswith("#EXTINF"):
            group=re.search(r'group-title="([^"]*)"',line,re.I)
            name=line.rsplit(",",1)[-1].strip() if "," in line else ""
            if not name:
                name_match=re.search(r'tvg-name="([^"]+)"',line,re.I);name=name_match.group(1).strip() if name_match else f"Canal {line_no}"
            meta={"name":name,"category":group.group(1).strip() if group and group.group(1).strip() else "Outros"}
        elif line.startswith("#EXTVLCOPT:"):
            option=line.partition(":")[2]
            if option.casefold().startswith("http-referrer="):referrer=option.split("=",1)[1]
            elif option.casefold().startswith("http-user-agent="):agent=option.split("=",1)[1]
        elif line.startswith("#"):
            continue
        elif valid_uri(line):
            if meta is None:meta={"name":f"Fonte {len(channels)+1}","category":"Outros"}
            quality=re.search(r"\b(4K|UHD|FHD|HD|SD|\d{3,4}p)\b",meta["name"],re.I)
            channels.append({"name":meta["name"],"category":meta["category"],"logo":"","sources":[{"url":line,"quality":quality.group(1).upper() if quality else "Automática","name":"M3U","referer":referrer,"agent":agent}]})
            meta=None;referrer="";agent=""
        else:
            warnings.append(f"Linha {line_no}: URI inválida ignorada.")
    if not channels:warnings.append("A playlist não contém entradas M3U válidas.")
    return dedupe_channels(channels),warnings


def valid_uri(uri: str) -> bool:
    try:
        parts = urlsplit(uri)
        return parts.scheme.casefold() in SUPPORTED_SCHEMES and (parts.scheme.casefold() not in {"http", "https"} or bool(parts.netloc))
    except ValueError:
        return False


def probe_source(src: dict) -> dict:
   
    url = src.get("url", "").strip()
    scheme = urlsplit(url).scheme.casefold() if valid_uri(url) else ""
    if scheme not in {"http", "https"}:
        return {"state":"unknown", "ms":None, "reason":"Protocolo sem teste HTTP; valide no VLC."}
    headers = {"User-Agent": src.get("agent") or "OrbitTV/1.0 (stream health check)", "Range":"bytes=0-4095"}
    if src.get("referer"):
        headers["Referer"] = src["referer"]
    start = time.monotonic()
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            code = resp.status
            resp.read(4096)
        elapsed = (time.monotonic()-start)*1000
        if 200 <= code < 400:
            return {"state":"ok", "ms":elapsed, "reason":f"HTTP {code}; resposta inicial recebida"}
        return {"state":"bad", "ms":elapsed, "reason":f"HTTP {code}"}
    except urllib.error.HTTPError as exc:
        return {"state":"bad", "ms":(time.monotonic()-start)*1000, "reason":f"HTTP {exc.code}: {http_reason(exc.code)}"}
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        return {"state":"bad", "ms":None, "reason":network_reason(reason)}
    except TimeoutError:
        return {"state":"bad", "ms":None, "reason":"Tempo esgotado (mais de 5 s)"}
    except Exception as exc:
        return {"state":"bad", "ms":None, "reason":network_reason(str(exc))}


def http_reason(code: int) -> str:
    return {401:"autenticação necessária",403:"acesso negado",404:"endereço não encontrado",408:"tempo esgotado",410:"fonte removida",429:"limite de pedidos",500:"erro do servidor",502:"gateway indisponível",503:"serviço indisponível",504:"gateway expirou"}.get(code,"resposta recusada pelo servidor")


def network_reason(text: str) -> str:
    s=text.casefold()
    if "timed out" in s or "timeout" in s: return "Timeout: servidor não respondeu no limite de 5 s"
    if "name or service not known" in s or "getaddrinfo failed" in s or "nodename nor servname" in s: return "DNS: domínio não encontrado"
    if "certificate" in s or "ssl" in s: return "Erro de certificado/TLS"
    if "refused" in s: return "Conexão recusada pelo servidor"
    if "reset" in s: return "Conexão interrompida pelo servidor"
    return "Falha de rede: " + text[:130]


def classify_vlc_log(text: str) -> str | None:
    t=text.casefold()
    checks=[(("403", "forbidden"),"HTTP 403: acesso negado pelo servidor"), (("401", "unauthorized"),"HTTP 401: autenticação necessária"), (("404", "not found"),"HTTP 404: endereço ou playlist não encontrado"), (("timed out", "timeout"),"Timeout: o servidor não respondeu"), (("could not resolve", "name resolution", "getaddrinfo"),"DNS: falha ao localizar o servidor"), (("certificate", "tls", "ssl error"),"Falha de certificado/TLS"), (("no suitable access module", "access_demux"),"O VLC não encontrou um módulo compatível para esta fonte"), (("invalid data", "invalid playlist", "cannot recognize"),"Formato ou playlist inválida/não reconhecida"), (("connection refused",),"Conexão recusada pelo servidor"), (("no access", "cannot open"),"O VLC não conseguiu acessar a fonte; verifique estado, URL e cabeçalhos")]
    for needles, message in checks:
        if any(n in t for n in needles): return message
    return None


class OrbitTV(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Orbit TV · Live Player")
        self.geometry("1220x800")
        self.minsize(980,650)
        self.configure(fg_color=BG)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.channels=[]; self.selected=0; self.sources=[]; self.source_indices=[]; self.selected_source_index=None; self.source_row_by_index={}
        self.probe_cache={}; self.probe_running=False; self.probe_worker_busy=False; self.probe_token=0
        self.ui_events=queue.Queue()
        self.logo_executor=ThreadPoolExecutor(max_workers=3)
        self.logo_pending=set();self.logo_failed=set();self.logo_images={};self.channel_widgets={};self.channel_thumbs={}
        self.last_vlc_log_offset=0
        self.search_after_id=None; self.filtered_indices=[]; self.visible_channel_count=60
        self.search_var=tk.StringVar(); self.category_var=tk.StringVar(value="Todas as categorias")
        self.category_display_to_name={};self.category_name_to_display={};self.category_counts={}
        self.search_var.trace_add("write",lambda *_:self.schedule_search_refresh())
        self.setup_space_background(); self.build_ui(); self.load_catalog(CATALOG_PATH,quiet=True)
        self.after(700,self.check_vlc_log)
        self.after(LIVE_REFRESH_SECONDS*1000,self.live_refresh)
        self.after(80,self._drain_ui_events)
        self.after(120,self._pulse_source_card)
        self.protocol("WM_DELETE_WINDOW",self.on_close)
        self.bind_all("<MouseWheel>",self._on_mousewheel,add="+")
        self.bind_all("<Button-4>",self._on_mousewheel,add="+")
        self.bind_all("<Button-5>",self._on_mousewheel,add="+")

    def _drain_ui_events(self):
        
        if not self.winfo_exists():return
        for _ in range(16):
            try:event=self.ui_events.get_nowait()
            except queue.Empty:break
            kind=event[0]
            if kind=="probe_result":
                _,token,source,result,done,total=event
                if token==self.probe_token:
                    self.probe_cache[self.cache_key(source)]=result
                    self._probe_progress(token,done,total)
            elif kind=="probe_done":self._probe_done(event[1])
            elif kind=="vlc_error":self._report_vlc_failure(event[1],event[2],event[3])
            elif kind=="logo":self._apply_logo(event[1],event[2])
        self.after(80,self._drain_ui_events)

    def _pulse_source_card(self):
        if not self.winfo_exists():return
        row=self.source_row_by_index.get(self.selected_source_index)
        if row is not None and row.winfo_exists():
            strength=.5+.5*math.sin(time.monotonic()*2.0)
            low=(53,68,91);high=(93,113,158)
            color="#"+"".join(f"{round(low[i]+(high[i]-low[i])*strength):02x}" for i in range(3))
            row.configure(border_color=color)
        self.after(120,self._pulse_source_card)

    def _request_logo(self,url):
        if not url or url in self.logo_images or url in self.logo_pending or url in self.logo_failed:return
        if urlsplit(url).scheme not in {"http","https"}:return
        self.logo_pending.add(url)
        self.logo_executor.submit(self._download_logo,url)

    def _download_logo(self,url):
        data=None
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"OrbitTV/1.0"})
            with urllib.request.urlopen(req,timeout=4) as response:
                raw=response.read(350000+1)
                if len(raw)<=350000 and (response.headers.get("Content-Type","").lower().startswith("image/") or raw.startswith((b"\x89PNG",b"\xff\xd8",b"GIF8",b"RIFF"))):data=raw
        except Exception:pass
        self.ui_events.put(("logo",url,data))

    def _apply_logo(self,url,data):
        self.logo_pending.discard(url)
        if not data or Image is None:
            self.logo_failed.add(url);return
        try:
            im=Image.open(io.BytesIO(data))
            if im.width>4096 or im.height>4096 or im.width*im.height>8_000_000:
                self.logo_failed.add(url);return
            im=im.convert("RGBA")
            im.thumbnail((36,36),Image.Resampling.LANCZOS)
            badge=ctk.CTkImage(light_image=im,dark_image=im,size=im.size)
        except Exception:
            self.logo_failed.add(url);return
        self.logo_images[url]=badge
        for i,widget in self.channel_widgets.items():
            if i<len(self.channels) and self.channels[i].get("logo")==url and widget.winfo_exists():
                try:
                    self.channel_thumbs[i].configure(image=badge,text="",fg_color="transparent")
                except Exception:pass

    def setup_space_background(self):
       
        self.space_canvas=tk.Canvas(self,bg=BG,highlightthickness=0)
        self.space_canvas.place(x=0,y=0,relwidth=1,relheight=1)
        self.space_canvas.tk.call("lower", self.space_canvas._w)
        self.space_stars=[(random.random(),random.random(),random.choice((1,1,1.5,2))) for _ in range(42)]
        self.scene_angle=0.0
        self.bind("<Configure>",self._resize_space)
        self.after_idle(self._draw_space_background)

    def _resize_space(self,event=None):
        if event and event.widget is self:
            self.space_canvas.configure(width=max(1,event.width),height=max(1,event.height))
            self._draw_space_background()

    def _draw_space_background(self):
        if not self.winfo_exists():return
        c=self.space_canvas; w=max(self.winfo_width(),1); h=max(self.winfo_height(),1)
        c.delete("stars")
        for i,(sx,sy,r) in enumerate(self.space_stars):
            x=sx*w;y=sy*h
            color="#29324A" if i%5 else "#39435E"
            c.create_oval(x-r,y-r,x+r,y+r,fill=color,outline="",tags="stars")
        c.create_oval(w*.68,h*.06,w*1.06,h*.47,outline="#111A2B",width=2,tags="stars")
        c.create_oval(w*.72,h*.09,w*1.03,h*.44,outline="#0E1727",width=1,tags="stars")

    def live_refresh(self):
        if not self.winfo_exists():return
        if self.sources and not self.probe_running:
            self.test_sources(silent=True,force=True)
        self.after(LIVE_REFRESH_SECONDS*1000,self.live_refresh)

    def add_button_motion(self,button,base="#202B40",hover="#374B70"):
    
        button._orbit_base=base; button._orbit_hover=hover
        button.bind("<Enter>",lambda _e,b=button:self._animate_button_color(b,b._orbit_base,b._orbit_hover),add="+")
        button.bind("<Leave>",lambda _e,b=button:self._animate_button_color(b,b._orbit_hover,b._orbit_base),add="+")

    def _animate_button_color(self,button,start,end):
        if not button.winfo_exists():return
        try:
            a=tuple(int(start[i:i+2],16) for i in (1,3,5)); b=tuple(int(end[i:i+2],16) for i in (1,3,5))
        except Exception:return
        def frame(n=0):
            if not button.winfo_exists():return
            t=min(n/7,1); col="#"+"".join(f"{round(a[i]+(b[i]-a[i])*t):02x}" for i in range(3))
            try:button.configure(fg_color=col,hover_color=col)
            except Exception:return
            if n<7:button.after(18,lambda:frame(n+1))
        frame()

    def animate_planet(self):
        if not hasattr(self,"planet_canvas") or not self.winfo_exists():return
        c=self.planet_canvas; w=max(c.winfo_width(),360); h=max(c.winfo_height(),110)
        if not getattr(self,"_blackhole_items",None):
            c.delete("all")
            # Static distressed star field, scratches and dust: create once.
            for i in range(38):
                x=((i*83+29)%997)/997*w; y=((i*47+11)%97)/97*h; r=.7+(i%3)*.35
                c.create_oval(x-r,y-r,x+r,y+r,fill="#596070" if i%5 else "#A37B70",outline="")
            for i in range(9):
                x=((i*131+37)%991)/991*w; y=((i*29+15)%89)/89*h
                c.create_line(x,y,x+7+(i%4)*3,y-1+(i%3),fill="#30323C",width=1)
            self._bh_back=c.create_oval(0,0,1,1,outline="#754C55",width=2)
            self._bh_disk1=c.create_oval(0,0,1,1,outline="#C07B56",width=5)
            self._bh_disk2=c.create_oval(0,0,1,1,outline="#D5A16E",width=2)
            self._bh_glow=c.create_oval(0,0,1,1,fill="#31232D",outline="#98636B",width=1)
            self._bh_core=c.create_oval(0,0,1,1,fill="#030408",outline="#07090D",width=2)
            self._bh_lens=c.create_oval(0,0,1,1,outline="#E1B28B",width=1)
            self._bh_jet1=c.create_line(0,0,0,0,fill="#755B69",width=2)
            self._bh_jet2=c.create_line(0,0,0,0,fill="#755B69",width=2)
            self._collapse_halo=c.create_oval(0,0,1,1,outline="#EFA77F",width=3,state="hidden")
            self._collapse_core=c.create_oval(0,0,1,1,fill="#FFE0B0",outline="#F2B184",width=1,state="hidden")
            self._collapse_ray1=c.create_line(0,0,0,0,fill="#EFA77F",width=2,state="hidden")
            self._collapse_ray2=c.create_line(0,0,0,0,fill="#EFA77F",width=2,state="hidden")
            self._bh_caption=c.create_text(18,15,anchor="nw",text="IPTV Open Source",fill="#B18C86",font=("Segoe UI",8,"bold"))
            self._bh_subcaption=c.create_text(18,32,anchor="nw",text="",fill="#697184",font=("Segoe UI",8))
            self._blackhole_items=True
        cx=w*.72; cy=h*.55; rx=min(w*.235,158); ry=min(h*.31,39); r=min(h*.30,39)
        c.coords(self._bh_back,cx-rx,cy-ry,cx+rx,cy+ry)
        c.coords(self._bh_disk1,cx-rx*.86,cy-ry*.66,cx+rx*.86,cy+ry*.66)
        c.coords(self._bh_disk2,cx-rx*.67,cy-ry*.42,cx+rx*.67,cy+ry*.42)
        c.coords(self._bh_glow,cx-r*1.25,cy-r*1.25,cx+r*1.25,cy+r*1.25)
        c.coords(self._bh_core,cx-r*.82,cy-r*.82,cx+r*.82,cy+r*.82)
        c.coords(self._bh_lens,cx-r*1.03,cy-r*1.03,cx+r*1.03,cy+r*1.03)
        c.coords(self._bh_jet1,cx,cy-r*1.1,cx+3,cy-h*.43)
        c.coords(self._bh_jet2,cx,cy+r*1.1,cx-3,cy+h*.43)
        # A compact event flare blooms every 16 s; no canvas items are allocated per frame.
        phase=time.monotonic()%16; active=phase<2.4
        if active:
            pulse=max(0.0,math.sin(phase/2.4*math.pi))
            sx=cx-rx*.78; sy=cy-ry*.92; rr=4+13*pulse
            c.itemconfigure(self._collapse_halo,state="normal",width=1+3*pulse)
            c.coords(self._collapse_halo,sx-rr,sy-rr,sx+rr,sy+rr)
            c.itemconfigure(self._collapse_core,state="normal")
            c.coords(self._collapse_core,sx-3-pulse*2,sy-3-pulse*2,sx+3+pulse*2,sy+3+pulse*2)
            c.itemconfigure(self._collapse_ray1,state="normal");c.coords(self._collapse_ray1,sx-rr*1.8,sy,sx+rr*1.8,sy)
            c.itemconfigure(self._collapse_ray2,state="normal");c.coords(self._collapse_ray2,sx,sy-rr*1.8,sx,sy+rr*1.8)
        else:
            for item in (self._collapse_halo,self._collapse_core,self._collapse_ray1,self._collapse_ray2):c.itemconfigure(item,state="hidden")
        # Keep the accretion band alive with a restrained rotating highlight.
        self.scene_angle=(self.scene_angle+.025)%(2*math.pi)
        c.itemconfigure(self._bh_disk1,outline="#D18B66" if int(self.scene_angle*4)%2 else "#A8675E")
        self.after(33,self.animate_planet)

    def build_ui(self):
        shell=ctk.CTkFrame(self,fg_color="transparent"); shell.pack(fill="both",expand=True,padx=22,pady=18)
        head=ctk.CTkFrame(shell,fg_color="transparent"); head.pack(fill="x",pady=(0,16))
        brand=ctk.CTkFrame(head,fg_color="transparent"); brand.pack(side="left")
        ctk.CTkLabel(brand,text="ORBIT",font=("Segoe UI",25,"bold"),text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(brand,text="LIVE PLAYER  ·  CATÁLOGO LOCAL",font=("Segoe UI",10,"bold"),text_color=MUTED).pack(anchor="w")
        self.status=ctk.CTkLabel(head,text="● PRONTO",text_color=CYAN,fg_color=PANEL,corner_radius=18,padx=15,pady=9); self.status.pack(side="right")
        body=ctk.CTkFrame(shell,fg_color="transparent"); body.pack(fill="both",expand=True)
        body.grid_columnconfigure(0,weight=0,minsize=330); body.grid_columnconfigure(1,weight=1); body.grid_rowconfigure(0,weight=1)
        left=ctk.CTkFrame(body,fg_color=PANEL,corner_radius=18,border_width=1,border_color=BORDER); left.grid(row=0,column=0,sticky="nsew",padx=(0,16))
        top=ctk.CTkFrame(left,fg_color="transparent"); top.pack(fill="x",padx=14,pady=(14,8))
        ctk.CTkLabel(top,text="CANAIS",font=("Segoe UI",12,"bold"),text_color=TEXT).pack(side="left")
        import_btn=ctk.CTkButton(top,text="Importar",width=90,height=30,corner_radius=9,fg_color="#293957",hover_color="#41577D",command=self.choose_catalog); import_btn.pack(side="right"); self.add_button_motion(import_btn,"#293957","#41577D")
        self.count_label=ctk.CTkLabel(left,text="Nenhum catálogo",text_color=MUTED); self.count_label.pack(anchor="w",padx=15,pady=(0,8))
        ctk.CTkEntry(left,textvariable=self.search_var,placeholder_text="⌕  Buscar canal",height=38,corner_radius=10).pack(fill="x",padx=12,pady=(0,8))
        ctk.CTkLabel(left,text="CATEGORIA",font=("Segoe UI",9,"bold"),text_color=MUTED).pack(anchor="w",padx=14,pady=(2,4))
        self.category_menu=ctk.CTkOptionMenu(left,variable=self.category_var,values=["Todas as categorias"],height=36,corner_radius=11,fg_color="#172235",button_color="#273A57",button_hover_color="#3D5578",dropdown_fg_color="#111827",dropdown_hover_color="#263650",text_color=TEXT,command=self.set_category_from_label); self.category_menu.pack(fill="x",padx=12,pady=(0,6))
        self.category_quick_bar=ctk.CTkFrame(left,fg_color="transparent");self.category_quick_bar.pack(fill="x",padx=10,pady=(0,3))
        self.filter_count_label=ctk.CTkLabel(left,text="",font=("Segoe UI",9),text_color=MUTED);self.filter_count_label.pack(anchor="w",padx=14,pady=(0,5))
        self.listbox=ctk.CTkScrollableFrame(left,fg_color="transparent",scrollbar_button_color="#34445E",scrollbar_button_hover_color="#536B91"); self.listbox.pack(fill="both",expand=True,padx=8,pady=(0,8))
        self.listbox._parent_canvas.configure(yscrollincrement=56)
        right=ctk.CTkFrame(body,fg_color=PANEL,corner_radius=18,border_width=1,border_color=BORDER); right.grid(row=0,column=1,sticky="nsew")
        right.grid_columnconfigure(0,weight=1)
        hero=ctk.CTkFrame(right,fg_color="#151E32",corner_radius=16); hero.pack(fill="x",padx=15,pady=15)
        self.planet_canvas=tk.Canvas(hero,height=132,bg="#0D1425",highlightthickness=0)
        self.planet_canvas.pack(fill="x",padx=10,pady=(10,5))
        self.after(120,self.animate_planet)
        hero_top=ctk.CTkFrame(hero,fg_color="transparent"); hero_top.pack(fill="x",padx=18,pady=(15,2))
        self.title_label=ctk.CTkLabel(hero_top,text="Importe um catálogo",font=("Segoe UI",23,"bold"),text_color=TEXT); self.title_label.pack(side="left")
        self.source_count=ctk.CTkLabel(hero_top,text="0 fontes",fg_color="#25314A",text_color=CYAN,corner_radius=12,padx=10,pady=5); self.source_count.pack(side="right")
        self.category_label=ctk.CTkLabel(hero,text="O estado e a latência são estimativas da conexão inicial.",text_color=MUTED,wraplength=690,justify="left"); self.category_label.pack(anchor="w",padx=18,pady=(2,14))
        actions=ctk.CTkFrame(right,fg_color="transparent"); actions.pack(fill="x",padx=16,pady=(0,9))
        self.check_button=ctk.CTkButton(actions,text="↻  Testar fontes deste canal",height=38,corner_radius=10,fg_color="#273652",hover_color="#35496B",command=self.test_sources); self.check_button.pack(side="left",fill="x",expand=True,padx=(0,7)); self.add_button_motion(self.check_button,"#273652","#35496B")
        self.sort_button=ctk.CTkButton(actions,text="Ordenar: recomendadas",height=38,corner_radius=10,fg_color="#202B40",hover_color="#34496A",command=self.toggle_sort); self.sort_button.pack(side="right"); self.add_button_motion(self.sort_button,"#202B40","#34496A")
        ctk.CTkLabel(right,text="FONTES DESTE CANAL",font=("Segoe UI",10,"bold"),text_color=MUTED).pack(anchor="w",padx=20,pady=(4,2))
        legend=ctk.CTkFrame(right,fg_color="transparent");legend.pack(anchor="w",padx=20,pady=(0,5))
        for color,label in ((GREEN,"Online"),(RED,"Falha"),(GREY,"Não testada")):
            ctk.CTkLabel(legend,text="●",text_color=color,font=("Segoe UI",11,"bold")).pack(side="left",padx=(0,3))
            ctk.CTkLabel(legend,text=label,text_color=MUTED,font=("Segoe UI",9)).pack(side="left",padx=(0,12))
        self.source_list=ctk.CTkScrollableFrame(right,fg_color="transparent",height=190,scrollbar_button_color="#34445E",scrollbar_button_hover_color="#536B91"); self.source_list.pack(fill="x",padx=14,pady=(0,4))
        self.source_list._parent_canvas.configure(yscrollincrement=44)
        self.detail=ctk.CTkLabel(right,text="Importe um catálogo local para ver as fontes.",text_color=MUTED,wraplength=700,justify="left"); self.detail.pack(anchor="w",padx=21,pady=(10,5))
        self.progress=ctk.CTkProgressBar(right,height=5,progress_color=CYAN); self.progress.pack(fill="x",padx=19,pady=(4,10)); self.progress.set(0)
        self.play_button=ctk.CTkButton(right,text="▶   Reproduzir fonte selecionada no VLC",height=46,corner_radius=12,fg_color=ACCENT,hover_color="#656CE6",font=("Segoe UI",12,"bold"),command=self.play); self.play_button.pack(fill="x",padx=18,pady=(2,8)); self.add_button_motion(self.play_button,ACCENT,"#656CE6")
        self.last_error=ctk.CTkLabel(right,text="",text_color=RED,wraplength=700,justify="left"); self.last_error.pack(anchor="w",padx=21,pady=(0,5))
        footer=ctk.CTkFrame(right,fg_color="transparent"); footer.pack(fill="x",padx=18,pady=(3,15))
        log_btn=ctk.CTkButton(footer,text="Abrir diagnóstico",height=32,fg_color="#202B40",hover_color="#34496A",command=self.open_log); log_btn.pack(side="left"); self.add_button_motion(log_btn,"#202B40","#34496A")
        ctk.CTkLabel(footer,text="Atualização automática: 90 s · teste leve, não mede estabilidade do vídeo.",text_color=MUTED,font=("Segoe UI",9),wraplength=480,justify="right").pack(side="right")

    def load_catalog(self,path:Path,quiet=False):
        if path.suffix.casefold() in {".m3u",".m3u8"}:
            channels,warnings=parse_m3u(path)
        else:
            channels,warnings=parse_catalog(path)
        if not channels:
            if path.suffix.casefold() in {".m3u",".m3u8"}:
                if not quiet:messagebox.showwarning("Playlist não importada","\n".join(warnings[:8]) or "Não foram encontradas entradas válidas.")
                return
            idx=BASE_DIR/"catalogo_canais.json"
            try:
                meta=json.loads(idx.read_text(encoding="utf-8")); channels=[{"name":x["name"],"category":x.get("category","Outros"),"logo":"","sources":[],"listed_qualities":x.get("qualities",[])} for x in meta]
            except (OSError,ValueError,KeyError,TypeError): channels=[]
            self.channels=channels; self.rebuild_categories()
            if channels: self.select_channel(0); self.count_label.configure(text=f"{len(channels)} canais · índice sem URLs")
            if not quiet: messagebox.showwarning("Catálogo sem fontes","O índice pode mostrar nomes/qualidades, mas são necessários URLs de fontes autorizadas no catalogo.txt para reproduzir.")
            return
        self.channels=channels; self.probe_cache.clear(); self.selected=0; self.rebuild_categories(); self.select_channel(0)
        count=sum(len(c["sources"]) for c in channels); self.count_label.configure(text=f"{len(channels)} canais · {count:,} fontes".replace(",",".")); self.status.configure(text=f"● {len(channels)} CANAIS",text_color=CYAN)
        if warnings and not quiet: messagebox.showwarning("Catálogo importado",f"{len(channels)} canais e {count} fontes. {len(warnings)} aviso(s); chaves DRM são ignoradas.")

    def rebuild_categories(self):
        self.category_counts={}
        for channel in self.channels:
            category=channel.get("category","Outros") or "Outros"
            self.category_counts[category]=self.category_counts.get(category,0)+1
        ordered=sorted(self.category_counts,key=lambda c:(-self.category_counts[c],c.casefold()))
        total=len(self.channels);all_label=f"Todas as categorias · {total}"
        self.category_display_to_name={all_label:None};self.category_name_to_display={}
        for category in ordered:
            display=f"{category} · {self.category_counts[category]}"
            self.category_display_to_name[display]=category;self.category_name_to_display[category]=display
        self.category_menu.configure(values=[all_label]+[self.category_name_to_display[c] for c in ordered])
        self.category_var.set(all_label)
        self.render_category_quick_filters()

    def set_category_from_label(self,label):
        self.category_var.set(label)
        self.refresh_list();self.render_category_quick_filters()

    def set_category(self,category):
        label=next((k for k,v in self.category_display_to_name.items() if v==category),None)
        if label:
            self.category_var.set(label);self.refresh_list();self.render_category_quick_filters()

    def render_category_quick_filters(self):
        if not hasattr(self,"category_quick_bar"):return
        for widget in self.category_quick_bar.winfo_children():widget.destroy()
        total=len(self.channels);ordered=sorted(self.category_counts,key=lambda c:(-self.category_counts[c],c.casefold()))[:2]
        current=self.category_display_to_name.get(self.category_var.get())
        choices=[(None,f"Todos · {total}")]+[(cat,f"{cat[:10]} · {self.category_counts[cat]}") for cat in ordered]
        for category,label in choices:
            active=category==current
            button=ctk.CTkButton(self.category_quick_bar,text=label,height=29,corner_radius=15,font=("Segoe UI",9,"bold"),fg_color="#2C6B68" if active else "#1B2637",hover_color="#345158",command=lambda c=category:self.set_category(c))
            button.pack(side="left",fill="x",expand=True,padx=2);self.add_button_motion(button,"#2C6B68" if active else "#1B2637","#345158")

    def choose_catalog(self):
        filename=filedialog.askopenfilename(title="Selecionar catálogo ou playlist",filetypes=[("Catálogo de texto","*.txt"),("Playlist M3U","*.m3u"),("Playlist M3U8","*.m3u8"),("Todos os arquivos","*.*")])
        if filename: self.load_catalog(Path(filename))

    def schedule_search_refresh(self):
        if self.search_after_id:
            try:self.after_cancel(self.search_after_id)
            except Exception:pass
        self.search_after_id=self.after(180,self.refresh_list)

    def _on_mousewheel(self,event):
        if not hasattr(self,"listbox"):return
        widget=self.winfo_containing(event.x_root,event.y_root)
        target=None
        for container in (self.listbox,getattr(self,"source_list",None)):
            probe=widget
            while probe is not None and probe is not container:
                probe=getattr(probe,"master",None)
            if probe is container:
                target=container;break
        if target is None:return
        if getattr(event,"num",None)==4: units=-3
        elif getattr(event,"num",None)==5: units=3
        else:
            delta=getattr(event,"delta",0)
            units=-max(1,round(abs(delta)/120))* (1 if delta>0 else -1) if delta else 0
        if units:target._parent_canvas.yview_scroll(units,"units")
        return "break"

    def refresh_list(self):
        if not hasattr(self,"listbox"):return
        self.search_after_id=None
        for w in self.listbox.winfo_children():w.destroy()
        self.channel_widgets={};self.channel_thumbs={}
        query=self.search_var.get().strip().casefold();cat=self.category_display_to_name.get(self.category_var.get())
        self.filtered_indices=[i for i,ch in enumerate(self.channels)
            if (cat is None or ch.get("category")==cat)
            and (not query or query in ch["name"].casefold() or query in ch.get("category","Outros").casefold())]
        self.visible_channel_count=60
        self._append_channel_rows()

    def _append_channel_rows(self):
        start=len(self.channel_widgets)
        end=min(self.visible_channel_count,len(self.filtered_indices))
        for pos in range(start,end):
            i=self.filtered_indices[pos]; ch=self.channels[i]; n=len(ch["sources"])
            base="#1B2637" if i==self.selected else "#111827"
            card=ctk.CTkFrame(self.listbox,fg_color=base,corner_radius=13,border_width=1,border_color="#35445E" if i==self.selected else "#1A2433")
            card.pack(fill="x",pady=3,padx=2)
            thumb=ctk.CTkLabel(card,text="".join(word[0] for word in ch["name"].split()[:2]).upper()[:2] or "TV",width=38,height=38,corner_radius=10,fg_color="#28364C",text_color="#C4D3E9",font=("Segoe UI",10,"bold"))
            thumb.pack(side="left",padx=(8,5),pady=6)
            logo=ch.get("logo","")
            if logo in self.logo_images:thumb.configure(image=self.logo_images[logo],text="",fg_color="transparent")
            text=f"{ch['name']}\n{ch.get('category','Outros')}   ·   {n} fontes"
            b=ctk.CTkButton(card,text=text,anchor="w",height=48,corner_radius=10,fg_color="transparent",hover_color="#26364D",text_color=TEXT,font=("Segoe UI",11),command=lambda k=i:self.select_channel(k))
            b.pack(side="left",fill="x",expand=True,padx=(0,5),pady=3)
            self.add_button_motion(b,"#1B2637" if i==self.selected else "#111827","#26364D")
            self.channel_widgets[i]=b;self.channel_thumbs[i]=thumb
            self._request_logo(logo)
        more=self.listbox.winfo_children()
        for w in more:
            if getattr(w,"_orbit_more",False):w.destroy()
        if end<len(self.filtered_indices):
            rest=len(self.filtered_indices)-end
            b=ctk.CTkButton(self.listbox,text=f"Carregar mais canais · {rest} restantes",height=38,fg_color="#202B40",hover_color="#34496A",command=self.show_more_channels)
            b._orbit_more=True; b.pack(fill="x",pady=(7,12))
        self.filter_count_label.configure(text=f"Mostrando {end} de {len(self.filtered_indices)} canais")

    def show_more_channels(self):
        for w in self.listbox.winfo_children():
            if getattr(w,"_orbit_more",False):w.destroy()
        self.visible_channel_count+=60
        self._append_channel_rows()

    def select_channel(self,index):
        if not self.channels:return
        self.selected=index; ch=self.channels[index]; self.sources=ch["sources"]
        self.selected_source_index=None; self.visible_source_count=32
        self.probe_token+=1; self.probe_running=False; self.progress.set(0); self.last_error.configure(text="")
        self.title_label.configure(text=ch["name"]); self.category_label.configure(text=f"{ch.get('category','Outros')}  ·  {len(self.sources)} fontes neste canal")
        self.source_count.configure(text=f"{len(self.sources)} fontes")
        self.render_sources(); self.refresh_list()
        if self.sources: self.test_sources(silent=True)

    def cache_key(self,src): return (src.get("url",""),src.get("referer",""),src.get("agent",""))

    def render_sources(self):
        for widget in self.source_list.winfo_children():widget.destroy()
        self.source_indices=sorted(range(len(self.sources)),key=lambda i:self.sort_key(self.sources[i]))
        self.source_row_by_index={}
        if not self.sources:
            self.detail.configure(text="Este canal não tem fontes no catálogo.",text_color=MUTED);return
        if self.selected_source_index not in self.source_indices:self.selected_source_index=self.source_indices[0]
        self._append_source_rows()
        self.set_source(self.selected_source_index)

    def _append_source_rows(self):
        shown=0
        for i in self.source_indices:
            if i in self.source_row_by_index:continue
            if shown>=self.visible_source_count-len(self.source_row_by_index):break
            src=self.sources[i]; result=self.probe_cache.get(self.cache_key(src),{})
            state=result.get("state","unknown"); color={"ok":GREEN,"bad":RED,"unknown":GREY}.get(state,GREY)
            latency=f"{result['ms']:.0f} ms" if result.get("ms") is not None else ("FALHA" if state=="bad" else "—")
            title=f"{src.get('quality','Automática')}   ·   {src.get('name') or 'Fonte '+str(i+1)}"
            reason=result.get("reason","Aguardando teste de conexão")
            row=ctk.CTkFrame(self.source_list,fg_color="#1B2638" if i==self.selected_source_index else "#111A29",corner_radius=12,border_width=1,border_color="#536B91" if i==self.selected_source_index else "#202B3E")
            row.pack(fill="x",padx=2,pady=3)
            ctk.CTkLabel(row,text="●",font=("Segoe UI",17,"bold"),text_color=color,width=28).pack(side="left",padx=(8,2),pady=5)
            text_box=ctk.CTkFrame(row,fg_color="transparent");text_box.pack(side="left",fill="x",expand=True,pady=4)
            ctk.CTkLabel(text_box,text=title,anchor="w",font=("Segoe UI",11,"bold"),text_color=TEXT).pack(anchor="w")
            ctk.CTkLabel(text_box,text=reason[:92],anchor="w",font=("Segoe UI",9),text_color=color).pack(anchor="w")
            ctk.CTkLabel(row,text=latency,fg_color="#263247",text_color=color,corner_radius=9,font=("Segoe UI",9,"bold"),padx=8,pady=5).pack(side="right",padx=8)
            for widget in (row,*row.winfo_children(),*text_box.winfo_children()):
                widget.bind("<Button-1>",lambda _e,index=i:self.set_source(index),add="+")
            self.source_row_by_index[i]=row;shown+=1
        if len(self.source_row_by_index)<len(self.source_indices):
            more=ctk.CTkButton(self.source_list,text=f"Mostrar mais fontes ({len(self.source_indices)-len(self.source_row_by_index)} restantes)",height=34,corner_radius=12,fg_color="#202B40",hover_color="#34496A",command=self.show_more_sources)
            more._orbit_more_sources=True;more.pack(fill="x",padx=2,pady=(5,8));self.add_button_motion(more,"#202B40","#34496A")

    def show_more_sources(self):
        for widget in self.source_list.winfo_children():
            if getattr(widget,"_orbit_more_sources",False):widget.destroy()
        self.visible_source_count+=32;self._append_source_rows()

    def set_source(self,index):
        if not isinstance(index,int) or not 0<=index<len(self.sources):return
        self.selected_source_index=index;src=self.sources[index];result=self.probe_cache.get(self.cache_key(src),{})
        state=result.get("state","unknown");color={"ok":GREEN,"bad":RED,"unknown":GREY}.get(state,GREY)
        latency=f" · {result['ms']:.0f} ms" if result.get("ms") is not None else ""
        reason=result.get("reason","Ainda não verificada")
        self.detail.configure(text=f"{state.upper()}{latency}  ·  {src.get('quality','Automática')}\n{reason}\n{src.get('url','')[:160]}",text_color=color)
        for i,row in self.source_row_by_index.items():
            row.configure(fg_color="#1B2638" if i==index else "#111A29",border_color="#536B91" if i==index else "#202B3E")

    def sort_key(self,src):
        result=self.probe_cache.get(self.cache_key(src),{})
        return (0 if result.get("state")=="ok" else 1 if result.get("state")=="unknown" else 2, result.get("ms") if result.get("ms") is not None else 999999, src.get("quality",""))

    def test_sources(self,silent=False,force=False):
        if not self.sources or self.probe_running:return
        if self.probe_worker_busy:
            self.pending_probe=(silent,force)
            return
        now=time.time()
        stale=[(i,s) for i,s in enumerate(self.sources) if force or now-self.probe_cache.get(self.cache_key(s),{}).get("checked",0)>CACHE_SECONDS]
        if not stale:
            self.render_sources()
            if not silent:self.status.configure(text="● RESULTADOS EM CACHE",text_color=CYAN)
            return
        self.probe_running=True; self.probe_worker_busy=True; self.probe_token+=1; token=self.probe_token
        self.check_button.configure(state="disabled",text=f"Testando 0/{len(stale)}…"); self.progress.set(0); self.status.configure(text="● TESTANDO CONEXÃO…",text_color=AMBER)
        threading.Thread(target=self._probe_batch,args=(stale,token),daemon=True).start()

    def _probe_batch(self,items,token):
        total=len(items); done=0
        with ThreadPoolExecutor(max_workers=min(8,total)) as pool:
            futures={pool.submit(probe_source,s): (i,s) for i,s in items}
            for future in as_completed(futures):
                i,src=futures[future]
                try: result=future.result()
                except Exception as exc: result={"state":"bad","ms":None,"reason":f"Falha no teste: {exc}"}
                result["checked"]=time.time(); done+=1
                self.ui_events.put(("probe_result",token,src,result,done,total))
        self.ui_events.put(("probe_done",token))

    def _probe_progress(self,token,done,total):
        if token!=self.probe_token:return
        self.progress.set(done/max(total,1)); self.check_button.configure(text=f"Testando {done}/{total}…")

    def _probe_done(self,token):
        self.probe_worker_busy=False
        if token!=self.probe_token:
            self.probe_running=False
            if self.sources:self.test_sources(silent=True,force=True)
            return
        self.probe_running=False; self.check_button.configure(state="normal",text="↻  Testar fontes deste canal"); self.progress.set(1); self.render_sources()
        good=sum(self.probe_cache.get(self.cache_key(s),{}).get("state")=="ok" for s in self.sources)
        bad=sum(self.probe_cache.get(self.cache_key(s),{}).get("state")=="bad" for s in self.sources)
        self.status.configure(text=f"● {good} ATIVAS · {bad} FALHAS",text_color=GREEN if good else RED)
        self.after(1800,lambda:self.progress.set(0) if token==self.probe_token else None)

    def toggle_sort(self):
        # The default view always ranks reachable, lower-latency sources first.
        self.render_sources(); self.status.configure(text="● MAIS RÁPIDAS NO TOPO",text_color=CYAN)

    def selected_source(self):
        i=self.selected_source_index
        return (i,self.sources[i]) if i is not None and 0<=i<len(self.sources) else (None,None)

    def play(self):
        i,src=self.selected_source()
        if not src:messagebox.showwarning("Sem fonte","Selecione uma fonte do catálogo.");return
        url=src.get("url","").strip()
        if not valid_uri(url):messagebox.showerror("Endereço inválido","URL vazia ou protocolo não suportado.");return
        vlc=find_vlc()
        if not vlc:messagebox.showerror("VLC não encontrado","Instale o VLC ou configure VLC_PATH.");return
        args=[vlc,"--no-one-instance","--no-video-title-show","--network-caching=3000","--file-caching=1500","--verbose=2"]
        if src.get("referer"):args.append("--http-referrer="+src["referer"])
        if src.get("agent"):args.append("--http-user-agent="+src["agent"])
        args.append(url); logfile=BASE_DIR/"orbit_vlc.log"
        try:
            offset=logfile.stat().st_size if logfile.exists() else 0
            with open(logfile,"a",encoding="utf-8",errors="replace") as log:
                log.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] Canal={self.channels[self.selected]['name']} | fonte={i+1} | qualidade={src.get('quality')}\n");log.flush()
                process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            threading.Thread(target=self._watch_vlc,args=(process,offset,self.cache_key(src)),daemon=True).start()
            self.last_error.configure(text="");self.status.configure(text="● VLC ABERTO · aguardando reprodução",text_color=CYAN)
        except (OSError,ValueError) as exc:messagebox.showerror("Não foi possível abrir o VLC",str(exc))

    def _watch_vlc(self,process,start_offset,source_key):
        path=BASE_DIR/"orbit_vlc.log"; offset=start_offset; accumulated=""
        try:
            while True:
                try:
                    with open(path,"r",encoding="utf-8",errors="replace") as f:
                        f.seek(offset); chunk=f.read(); offset=f.tell()
                except OSError: chunk=""
                if chunk:
                    accumulated=(accumulated+chunk)[-24000:]
                    issue=classify_vlc_log(accumulated)
                    if issue:
                        self.ui_events.put(("vlc_error",source_key,issue,process.poll()))
                        return
                code=process.poll()
                if code is not None:
                    if code != 0:
                        issue=classify_vlc_log(accumulated) or f"O VLC encerrou com código {code}"
                        self.ui_events.put(("vlc_error",source_key,issue,code))
                    return
                time.sleep(1.2)
        except Exception:
            return

    def _report_vlc_failure(self,source_key,issue,code):
        res={"state":"bad","ms":self.probe_cache.get(source_key,{}).get("ms"),"reason":"VLC: "+issue,"checked":time.time()}
        self.probe_cache[source_key]=res
        self.render_sources()
        suffix=f" (código VLC {code})" if code not in (None,0) else ""
        self.last_error.configure(text="● Reprodução falhou: "+issue+suffix+". Consulte o diagnóstico.",text_color=RED)
        self.status.configure(text="● FONTE COM FALHA",text_color=RED)

    def check_vlc_log(self):
        # Keep interface responsive; VLC diagnostics are checked after each launch.
        if self.winfo_exists():self.after(2500,self.check_vlc_log)

    def open_log(self):
        path=BASE_DIR/"orbit_vlc.log"
        if not path.exists():messagebox.showinfo("Sem diagnóstico","Ainda não há tentativas de reprodução.");return
        try:
            if sys.platform.startswith("win"):os.startfile(str(path))
            elif sys.platform=="darwin":subprocess.Popen(["open",str(path)])
            else:subprocess.Popen(["xdg-open",str(path)])
        except OSError as exc:messagebox.showerror("Erro abrindo diagnóstico",f"{exc}\n{path}")

    def on_close(self):
        try:self.logo_executor.shutdown(wait=False,cancel_futures=True)
        except Exception:pass
        self.destroy()

if __name__=="__main__":
    app=OrbitTV();app.mainloop()

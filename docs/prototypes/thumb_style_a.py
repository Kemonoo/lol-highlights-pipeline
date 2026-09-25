import sys, json
sys.argv=[sys.argv[0],"2026-09-24"]
import thumb_proto as tp
import thumb_variants as tv
from cam_snap import gray_frames, snap
from PIL import Image, ImageDraw, ImageFilter
W,H=tp.W,tp.H
RED=(225,6,0); YEL=(255,230,0); TURQ=(64,224,208)
CAP=tv.fit("Arial Black","QUADRA KILL",800,150)
cams=json.loads(tp.cache_p.read_text())
Z=1.25
def make(src,box,word,edge=TURQ,label="",m=40):
    cx,cy=tp.action_centre(src,box); cx,cy=0.5+(cx-0.5)*0.6,0.5+(cy-0.5)*0.6
    # crop window used by zoom(), to project the original webcam into thumbnail space
    cw,ch=1920/Z,1080/Z
    x0=min(max(cx*1920-cw/2,0),1920-cw); y0=min(max(cy*1080-ch/2,0),1080-ch)
    img=tp.pop(tp.zoom(src,Z,cx,cy))
    if box:
        x,y,bw,bh=box
        s=W/cw
        px0,py0=(x-x0)*s,(y-y0)*s; px1,py1=(x+bw-x0)*s,(y+bh-y0)*s
        r=(int(max(px0,0)),int(max(py0,0)),int(min(px1,W)),int(min(py1,H)))
        if r[2]>r[0] and r[3]>r[1]:                      # blur the original webcam away
            reg=img.crop(r).filter(ImageFilter.GaussianBlur(28))
            img.paste(reg,r[:2])
        cam=src.crop((int(x),int(y),int(x+bw),int(y+bh)))
        left=x+bw/2<960; top=y+bh/2<540
        # big enough to cover most of where the original sits, within limits
        w=int(min(max(420,(r[2]-r[0])-m+20),560)); h=int(w*bh/bw)
        if h>380: h=380; w=int(h*bw/bh)
        cam=tp.pop(cam.resize((w,h),Image.LANCZOS))
        e=7
        fr=Image.new("RGB",(w+2*e,h+2*e),edge); fr.paste(cam,(e,e))
        px=m if left else W-fr.width-m
        py=(H-fr.height-m) if not top else H-fr.height-m     # always bottom: text owns the top
        img.paste(fr,(px,py))
    d=ImageDraw.Draw(img)
    size=min(CAP,tv.fit("Arial Black",word,800,150)); f=tv.font("Arial Black",size)
    d.text((46,40),word,font=f,fill=YEL,stroke_width=max(6,size//14),stroke_fill=(0,0,0))
    d.rectangle((0,0,W-1,H-1),outline=RED,width=15)
    if label:
        lf=tv.ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf",26)
        d.text((W-24,24),label,font=lf,fill=(255,255,255),anchor="ra",stroke_width=3,stroke_fill=(0,0,0))
    return img
if __name__=="__main__":
    tiles=[]
    for c in tp.clips[:6]:
        mp4=tp.raw/f"{c['id']}.mp4"; b=cams.get(c["id"]); box=None
        if b:
            iw,ih=tp.streamer_cam._size(mp4) or (1920,1080)
            box=(b[0]*1920/iw,b[1]*1080/ih,b[2]*1920/iw,b[3]*1080/ih)
            d=float(c["duration"])
            box,_=snap(gray_frames(mp4,[d*k/7 for k in range(1,7)]),box)
        src=tp.best_frame(mp4,float(c.get("api_best_moment_s") or 0),float(c["duration"]),box)
        im=make(src,box,tp.word_for(c)); tiles.append(im)
        im.save(tp.OUT/"variants"/f"final_{c['broadcaster_name']}.jpg",quality=92)
    tv.sheet(tiles,"8_final_turquoise.jpg",cols=2)
    print("ok")

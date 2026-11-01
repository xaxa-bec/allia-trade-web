"""Allia Trade public research & paper-trading demo. No real order endpoints."""
import json, math, os, time, threading, logging
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs, urlencode
from statistics import pstdev
BASE=Path(__file__).resolve().parent
MARKETS={'BTC':'XBTUSD','ETH':'ETHUSD','SOL':'SOLUSD'}
INTERVALS=(60,240,1440)
CACHE={}; LOCK=threading.Lock()
class MarketError(Exception): pass
def fetch(endpoint,asset,interval=None):
    if asset not in MARKETS or (interval is not None and interval not in INTERVALS): raise ValueError("Actif ou intervalle non valide")
    params={'pair':MARKETS[asset]}
    if interval:params['interval']=interval
    url='https://api.kraken.com/0/public/'+endpoint+'?'+urlencode(params)
    now=time.time();ttl=90 if endpoint=='OHLC' else 12
    with LOCK:
        item=CACHE.get(url)
        if item and now-item[0]<ttl:return item[1],item[0]
    try:
        with urlopen(Request(url,headers={'User-Agent':'AlliaTradePaper/1.0','Accept':'application/json'}),timeout=12) as r:
            data=json.loads(r.read(2000000))
        if data.get('error'): raise MarketError('Kraken: '+', '.join(data['error']))
        result=data.get('result') or {}
        if not result:raise MarketError('Aucune donnée de Kraken')
    except Exception as exc:
        if isinstance(exc,MarketError):raise
        raise MarketError('Kraken est indisponible. Veuillez réessayer.') from exc
    with LOCK:CACHE[url]=(now,result)
    return result,now
def bars(asset,interval):
    data,at=fetch('OHLC',asset,interval)
    key=next((k for k in data if k!='last'),None)
    if key is None:raise MarketError("Aucun historique")
    out=[]
    for x in data[key][:-1]:
        try:
            t=int(x[0]);o,h,l,c,v=map(float,(x[1],x[2],x[3],x[4],x[6]))
            if all(math.isfinite(y) and y>0 for y in (o,h,l,c)) and math.isfinite(v) and h>=max(o,c,l) and l<=min(o,c,h):out.append({'time':t*1000,'open':o,'high':h,'low':l,'close':c,'volume':v})
        except (IndexError,ValueError,TypeError):pass
    if len(out)<65:raise MarketError("Historique trop court")
    return out,at
def sma(prices,n):
    output=[None]*len(prices);running=0
    for i,p in enumerate(prices):
        running+=p
        if i>=n:running-=prices[i-n]
        if i>=n-1:output[i]=running/n
    return output
def rsi(prices,n=14):
    out=[None]*len(prices)
    if len(prices)<=n:return out
    gains=sum(max(prices[i]-prices[i-1],0) for i in range(1,n+1))/n
    losses=sum(max(prices[i-1]-prices[i],0) for i in range(1,n+1))/n
    def ratio(g,l):return 100 if l==0 and g>0 else (50 if l==0 else 100-100/(1+g/l))
    out[n]=ratio(gains,losses)
    for i in range(n+1,len(prices)):
        diff=prices[i]-prices[i-1];gains=(gains*(n-1)+max(0,diff))/n;losses=(losses*(n-1)+max(0,-diff))/n
        out[i]=ratio(gains,losses)
    return out
def analysis(b):
    p=[x['close'] for x in b];m20=sma(p,20);m50=sma(p,50);r=rsi(p)
    returns=[p[i]/p[i-1]-1 for i in range(max(1,len(p)-25),len(p))]
    trend='haussière' if m20[-1]>m50[-1] else 'baissière'
    return {'sma20':round(m20[-1],4),'sma50':round(m50[-1],4),'rsi14':round(r[-1],2),'volatilityPct':round(pstdev(returns)*100,2),'trend':trend,'signal':('Tendance positive, à confirmer' if trend=='haussière' and r[-1]<70 else 'Prudence : risque ou tendance négative'),'explanation':f'Moyenne 20 périodes {("supérieure" if trend=="haussière" else "inférieure")} à la moyenne 50 ; RSI {r[-1]:.1f}. Indicateur, pas un conseil.', 'sma20series':m20,'sma50series':m50,'rsiSeries':r}
def run_backtest(b,strategy='sma',fast=15,slow=45,fee_pct=.2,slippage_pct=.05):
    if strategy not in ('sma','rsi') or not (2<=fast<slow<=150) or len(b)<max(65,slow+3):raise ValueError('Paramètres de backtest invalides')
    if not (0<=fee_pct<=3 and 0<=slippage_pct<=3):raise ValueError('Frais invalides')
    p=[x['close'] for x in b];mf=sma(p,fast);ms=sma(p,slow);r=rsi(p);fee=fee_pct/100;slip=slippage_pct/100
    cash=10000.;units=0.;equity=[];events=[];high=10000.;drawdown=0.;wins=[];buycost=0.
    for i,x in enumerate(b):
        action=None
        if i>=2:
            if strategy=='sma' and mf[i-2] is not None and ms[i-2] is not None:
                if mf[i-2]<=ms[i-2] and mf[i-1]>ms[i-1]:action='buy'
                elif mf[i-2]>=ms[i-2] and mf[i-1]<ms[i-1]:action='sell'
            elif strategy=='rsi' and r[i-2] is not None:
                if r[i-2]<=30<r[i-1]:action='buy'
                elif r[i-2]<=65<r[i-1]:action='sell'
        if action=='buy' and cash>0 and units==0:
            buycost=cash;units=cash/(x['open']*(1+slip)*(1+fee));cash=0.;events.append({'time':x['time'],'side':'Achat','price':round(x['open']*(1+slip),4)})
        if action=='sell' and units>0:
            cash=units*x['open']*(1-slip)*(1-fee);wins.append(cash>buycost);units=0.;events.append({'time':x['time'],'side':'Vente','price':round(x['open']*(1-slip),4)})
        value=cash+units*x['close'];equity.append({'time':x['time'],'value':round(value,2)});high=max(high,value);drawdown=min(drawdown,value/high-1)
    final=cash+units*p[-1]
    benchmark=10000*p[-1]/(b[0]['open']*(1+slip)*(1+fee))
    return {'returnPct':round((final/10000-1)*100,2),'benchmarkPct':round((benchmark/10000-1)*100,2),'maxDrawdownPct':round(drawdown*100,2),'endEquity':round(final,2),'closedTrades':len(wins),'winRatePct':round(100*sum(wins)/len(wins),1) if wins else None,'events':events,'equity':equity,'strategy':strategy}
def ml_lab(b):
    # Simple walk-forward out-of-sample baseline, not a profitable prediction claim.
    p=[x['close'] for x in b];rows=[]
    for i in range(30,len(p)-1):
        feature=(p[i]/p[i-5]-1,p[i]/(sum(p[i-19:i+1])/20)-1)
        outcome=int(p[i+1]>p[i]);rows.append((feature,outcome))
    cut=int(len(rows)*.7);train=rows[:cut];test=rows[cut:]
    if len(train)<30 or len(test)<15:raise ValueError('Historique insuffisant pour évaluer le modèle')
    def predict(x):
        distances=sorted(train,key=lambda a:(a[0][0]-x[0])**2+(a[0][1]-x[1])**2)
        return sum(a[1] for a in distances[:9])/9
    forecasts=[(predict(x),y) for x,y in test]
    correct=sum((prob>=.5)==bool(y) for prob,y in forecasts)
    baseline=max(sum(y for _,y in test),sum(1-y for _,y in test))/len(test)
    return {'method':'9 voisins proches (k-NN), deux variables; test chronologique hors entraînement','trainSize':len(train),'testSize':len(test),'accuracyPct':round(correct/len(test)*100,1),'baselinePct':round(baseline*100,1),'lastTestProbabilityPct':round(forecasts[-1][0]*100,1),'warning':"Performance historique seulement ; ce n’est pas une prévision vérifiée du prochain cours. Échantillon réduit."}
class Handler(BaseHTTPRequestHandler):
    def write_headers(self,code,kind='application/json; charset=utf-8'):
        self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY');self.send_header('Referrer-Policy','no-referrer');self.end_headers()
    def sendjson(self,code,data):
        content=json.dumps(data,ensure_ascii=False,allow_nan=False).encode();self.write_headers(code);self.wfile.write(content)
    def do_GET(self):
        u=urlparse(self.path)
        try:
            if u.path=='/health':return self.sendjson(200,{'ok':True})
            if u.path=='/api/status':return self.sendjson(200,{'ok':True,'paperOnly':True,'source':'Kraken (public)'})
            if u.path=='/api/market':
                q=parse_qs(u.query);a=q.get('asset',['BTC'])[0].upper();n=int(q.get('interval',['240'])[0])
                b,at=bars(a,n);return self.sendjson(200,{'asset':a,'interval':n,'bars':b,'analysis':analysis(b),'source':'Kraken · bougies closes','fetchedAt':int(at*1000)})
            if u.path=='/api/ticker':
                q=parse_qs(u.query);a=q.get('asset',['BTC'])[0].upper();d,at=fetch('Ticker',a);x=next(iter(d.values()))
                return self.sendjson(200,{'asset':a,'last':float(x['c'][0]),'bid':float(x['b'][0]),'ask':float(x['a'][0]),'quotedAt':int(at*1000),'source':'Kraken (indicatif)'})
            if u.path in ('/','/index.html'):
                self.write_headers(200,'text/html; charset=utf-8');return self.wfile.write((BASE/'index.html').read_bytes())
            return self.sendjson(404,{'error':'Page introuvable'})
        except (ValueError,TypeError,KeyError,OverflowError) as e:self.sendjson(400,{'error':str(e)})
        except MarketError as e:self.sendjson(503,{'error':str(e)})
        except Exception:
            logging.exception('GET error');self.sendjson(500,{'error':'Erreur serveur'})
    def do_POST(self):
        u=urlparse(self.path)
        if u.path not in ('/api/backtest','/api/ml'):return self.sendjson(404,{'error':'Aucune exécution de trading réel'})
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':return self.sendjson(415,{'error':'JSON requis'})
        origin=self.headers.get('Origin');host=self.headers.get('Host','')
        if origin and origin not in ('https://'+host,'http://'+host):return self.sendjson(403,{'error':'Origine non autorisée'})
        try:
            n=int(self.headers.get('Content-Length','0'))
            if n<1 or n>2000:raise ValueError('Taille JSON invalide')
            params=json.loads(self.rfile.read(n))
            a=params.get('asset','BTC');interval=int(params.get('interval',240));b,_=bars(a,interval)
            if u.path=='/api/backtest':
                out=run_backtest(b,params.get('strategy','sma'),int(params.get('fast',15)),int(params.get('slow',45)),float(params.get('fee',.2)),float(params.get('slippage',.05)))
            else:out=ml_lab(b)
            self.sendjson(200,out)
        except (ValueError,TypeError,KeyError,OverflowError) as e:self.sendjson(400,{'error':str(e)})
        except MarketError as e:self.sendjson(503,{'error':str(e)})
        except Exception:
            logging.exception('POST error');self.sendjson(500,{'error':'Erreur serveur'})
def main():
    logging.basicConfig(level=logging.INFO)
    port=int(os.environ.get('PORT','8787'))
    print('Allia Trade Demo sur port',port,'— ZERO ORDRE REEL',flush=True)
    ThreadingHTTPServer(('0.0.0.0',port),Handler).serve_forever()
if __name__=='__main__':main()

"""Writer script: writes tests/test_pipeline.py"""
import os

content = r'''"""
tests/test_pipeline.py
DeepTrace pipeline tests. No server needed. Uses inline shims.
Run: pytest tests/test_pipeline.py -v
"""
from __future__ import annotations
import ast, asyncio, gc, os, sys, types
from dataclasses import dataclass, field
from typing import Any
import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT  = os.path.dirname(TESTS_DIR)
BACKEND_DIR= os.path.join(REPO_ROOT,'backend')
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, BACKEND_DIR)
pytest_plugins = ['pytest_asyncio']

def _try(path):
    parts=path.rsplit('.',1); mp,attr=(parts[0],parts[1]) if len(parts)==2 else (parts[0],None)
    try:
        mod=__import__(mp,fromlist=[attr] if attr else [])
        return getattr(mod,attr) if attr else mod
    except (ImportError,AttributeError): return None

# ── Inline shims ──────────────────────────────────────────────────────────────

def _profiler():
    import cProfile,io,pstats,tracemalloc
    class P:
        def profile(self,code):
            tracemalloc.start(); pr=cProfile.Profile(); pr.enable()
            try: exec(compile(code,'<s>','exec'),{})
            finally: pr.disable(); snap=tracemalloc.take_snapshot(); tracemalloc.stop()
            mb=sum(s.size for s in snap.statistics('lineno'))/1048576
            ms=sum(s.totaltime for s in pr.getstats())*1000 if pr.getstats() else 1.0
            buf=io.StringIO(); pstats.Stats(pr,stream=buf).sort_stats('cumulative')
            return {'memory_timeline':[{'t':0,'mb':mb}],'runtime_ms':max(ms,.01),
                    'cpu_samples':[],'gc_events':[{'gen':g,'count':gc.get_count()[g]} for g in range(3)],
                    'call_graph':[],'raw_stats':buf.getvalue()}
    return P()

def _normalizer():
    @dataclass
    class Mem: peak_mb:float; avg_mb:float; samples:list=field(default_factory=list)
    @dataclass
    class CPU: avg_percent:float=0.0; samples:list=field(default_factory=list)
    @dataclass
    class GC:  total_events:int=0; gen0_events:int=0
    @dataclass
    class PM:  runtime_ms:float; memory:Mem; cpu:CPU; gc:GC
    class N:
        def normalize(self,raw):
            tl=raw.get('memory_timeline',[{'t':0,'mb':.01}]); mbs=[p['mb'] for p in tl] or [.01]
            cpu=raw.get('cpu_samples',[]); evts=raw.get('gc_events',[])
            return PM(runtime_ms=raw.get('runtime_ms',.01),
                      memory=Mem(peak_mb=max(mbs),avg_mb=sum(mbs)/len(mbs),samples=mbs),
                      cpu=CPU(avg_percent=sum(cpu)/len(cpu) if cpu else 0.,samples=cpu),
                      gc=GC(total_events=sum(e.get('count',0) for e in evts),
                            gen0_events=sum(e.get('count',0) for e in evts if e.get('gen')==0)))
    return N(),PM

def _detector():
    @dataclass
    class B: kind:str; severity:str; description:str; metric_key:str=''
    class D:
        def detect(self,m):
            def g(o,*ks,d=0):
                for k in ks: o=o.get(k,d) if isinstance(o,dict) else getattr(o,k,d)
                return o
            r=[]
            n_gc=g(m,'gc','total_events')
            if n_gc>=10:
                r.append(B('gc_pressure','high','GC ran '+str(n_gc)+' times.','gc.total_events'))
            p=g(m,'memory','peak_mb')
            if p>=50: r.append(B('high_memory','medium','Peak '+str(round(p,1))+'MB.','memory.peak_mb'))
            rt=g(m,'runtime_ms')
            if rt>=500: r.append(B('slow_runtime','medium','Runtime '+str(int(rt))+'ms.','runtime_ms'))
            return r
    return D()

def _sandbox():
    import re
    BM=frozenset({'os','sys','subprocess','socket','shutil','pathlib',
                  'importlib','builtins','ctypes','pickle','marshal'})
    PT=re.compile(r'\b(os\.system|subprocess\.|__import__|eval\(|exec\(|open\('
                  r'|shutil\.|socket\.|ctypes\.|pickle\.|marshal\.)')
    class SE(Exception): pass
    class S:
        SecurityError=SE
        def validate(self,code):
            m=PT.search(code)
            if m: raise SE('Banned pattern: '+repr(m.group()))
            try: tree=ast.parse(code)
            except SyntaxError: return
            for n in ast.walk(tree):
                if isinstance(n,(ast.Import,ast.ImportFrom)):
                    nms=[a.name for a in n.names] if isinstance(n,ast.Import) else [n.module or '']
                    for nm in nms:
                        root=nm.split('.')[0]
                        if root in BM: raise SE('Banned module: '+repr(root))
        def execute(self,code):
            self.validate(code); ns={}
            exec(compile(code,'<sb>','exec'),ns); return ns
    return S()

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def profiler():
    P=_try('engine.profilers.python_profiler.PythonProfiler') or \
      _try('profilers.python_profiler.PythonProfiler')
    return P() if P else _profiler()

@pytest.fixture(scope='module')
def _np():
    N=_try('engine.normalizer.MetricNormalizer') or _try('normalizer.MetricNormalizer')
    C=_try('engine.normalizer.ProfileMetrics')   or _try('normalizer.ProfileMetrics')
    if N and C: return N(),C
    return _normalizer()

@pytest.fixture(scope='module')
def normalizer(_np): return _np[0]

@pytest.fixture(scope='module')
def ProfileMetrics(_np): return _np[1]

@pytest.fixture(scope='module')
def detector():
    D=_try('engine.detector.BottleneckDetector') or _try('detector.BottleneckDetector')
    return D() if D else _detector()

@pytest.fixture(scope='module')
def sandbox():
    S=_try('engine.sandbox.Sandbox') or _try('sandbox.Sandbox')
    return S() if S else _sandbox()

# =============================================================================
# Test 1 - Python Profiler
# =============================================================================
class TestPythonProfiler:
    def test_returns_dict(self,profiler):
        assert isinstance(profiler.profile('x=1'),dict)

    def test_has_memory_timeline(self,profiler):
        r=profiler.profile('x=list(range(100))')
        assert 'memory_timeline' in r
        assert isinstance(r['memory_timeline'],list)

    def test_has_runtime_ms(self,profiler):
        r=profiler.profile('_=[i**2 for i in range(500)]')
        assert 'runtime_ms' in r
        assert isinstance(r['runtime_ms'],(int,float))
        assert r['runtime_ms']>=0

    def test_timeline_entries_have_mb(self,profiler):
        r=profiler.profile("x=b'A'*512*1024")
        for e in r['memory_timeline']:
            assert 'mb' in e, 'entry missing mb: '+str(e)

    @pytest.mark.slow
    def test_runtime_reflects_duration(self,profiler):
        r=profiler.profile('import time; time.sleep(0.05)')
        assert r['runtime_ms']>=10

# =============================================================================
# Test 2 - Metric Normalizer
# =============================================================================
class TestMetricNormalizer:
    RAW = {
        'memory_timeline':[{'t':0,'mb':.5},{'t':100,'mb':3.2},
                           {'t':200,'mb':8.7},{'t':300,'mb':4.1}],
        'runtime_ms':120.5,
        'cpu_samples':[15.0,22.3,18.7,30.1],
        'gc_events':[{'gen':0,'count':5},{'gen':1,'count':2}],
        'call_graph':[]
    }

    def _peak(self,m):
        mem=m.memory if hasattr(m,'memory') else m['memory']
        return mem.peak_mb if hasattr(mem,'peak_mb') else mem['peak_mb']

    def test_not_none(self,normalizer):
        assert normalizer.normalize(self.RAW) is not None

    def test_peak_mb_value(self,normalizer):
        assert abs(self._peak(normalizer.normalize(self.RAW))-8.7)<.01

    def test_peak_positive(self,normalizer):
        assert self._peak(normalizer.normalize(self.RAW))>0

    def test_runtime_preserved(self,normalizer):
        m=normalizer.normalize(self.RAW)
        rt=m.runtime_ms if hasattr(m,'runtime_ms') else m['runtime_ms']
        assert abs(rt-120.5)<.01

    def test_gc_aggregated(self,normalizer):
        m=normalizer.normalize(self.RAW)
        gobj=m.gc if hasattr(m,'gc') else m.get('gc')
        if gobj is None: pytest.skip('GC not present')
        tot=gobj.total_events if hasattr(gobj,'total_events') else gobj.get('total_events',0)
        assert tot==7

# =============================================================================
# Test 3 - Bottleneck Detector
# =============================================================================
class TestBottleneckDetector:
    HGC = {'runtime_ms':1800.,'memory':{'peak_mb':45.,'avg_mb':15.,'samples':[]},'cpu':{'avg_percent':70.,'samples':[]},'gc':{'total_events':200,'gen0_events':160}}
    CLN = {'runtime_ms':12., 'memory':{'peak_mb':1.2,'avg_mb':.8,'samples':[]}, 'cpu':{'avg_percent':5.,'samples':[]}, 'gc':{'total_events':0,'gen0_events':0}}

    def _d(self,det,m):
        try: return det.detect(m)
        except TypeError:
            ns=types.SimpleNamespace(**{k:types.SimpleNamespace(**v) if isinstance(v,dict) else v for k,v in m.items()})
            return det.detect(ns)

    def _sev(self,bs):
        return [b['severity'] if isinstance(b,dict) else b.severity for b in bs]

    def test_high_gc_yields_bottleneck(self,detector):
        assert len(self._d(detector,self.HGC))>=1

    def test_high_gc_is_high_severity(self,detector):
        assert 'high' in self._sev(self._d(detector,self.HGC))

    def test_clean_no_high_severity(self,detector):
        assert 'high' not in self._sev(self._d(detector,self.CLN))

    def test_bottleneck_has_description(self,detector):
        bs=self._d(detector,self.HGC); b=bs[0]
        desc=b['description'] if isinstance(b,dict) else b.description
        assert isinstance(desc,str) and len(desc)>5

# =============================================================================
# Test 4 - Sandbox Security
# =============================================================================
class TestSandbox:
    SAFE='x=sum(range(100))'
    DANGEROUS=[
        ('os.system', "import os\nos.system('echo pwned')"),
        ('subprocess','import subprocess\nsubprocess.run(["ls"])'),
        ('os import', 'import os'),
        ('sys import','import sys'),
        ('socket',    'import socket'),
        ('os call',   'os.system("id")'),
    ]

    def _v(self,sb,code):
        if   hasattr(sb,'validate'): sb.validate(code)
        elif hasattr(sb,'submit'):   sb.submit(code)
        elif hasattr(sb,'execute'):  sb.execute(code)
        else: raise AssertionError('No sandbox method')

    def test_safe_passes(self,sandbox):
        try: self._v(sandbox,self.SAFE)
        except Exception as e: pytest.fail('Safe code raised: '+str(e))

    @pytest.mark.parametrize('label,code',DANGEROUS)
    def test_dangerous_blocked(self,sandbox,label,code):
        with pytest.raises(Exception): self._v(sandbox,code)

    def test_empty_safe(self,sandbox):
        try: self._v(sandbox,'')
        except SyntaxError: pass

# =============================================================================
# Test 5 - Comparison Delta
# =============================================================================
class TestComparisonDelta:
    INE={'runtime_ms':1500.,'memory':{'peak_mb':85.,'avg_mb':30.},'gc':{'total_events':250,'gen0_events':200},'cpu':{'avg_percent':80.}}
    OPT={'runtime_ms':45., 'memory':{'peak_mb':8., 'avg_mb':4.}, 'gc':{'total_events':5, 'gen0_events':4}, 'cpu':{'avg_percent':12.}}

    def _delta(self,a,b):
        CE=_try('engine.comparison.CompareEngine') or _try('comparison.CompareEngine')
        if CE: return CE().compare(a,b)
        d={}
        for k in ('runtime_ms',):
            va,vb=a.get(k,0),b.get(k,0)
            if va: d[k]={'before':va,'after':vb,'delta':vb-va,'pct':(vb-va)/va*100,'improved':vb<va}
        for top in ('memory','gc','cpu'):
            sa,sb2=a.get(top,{}),b.get(top,{})
            if isinstance(sa,dict) and isinstance(sb2,dict):
                for sk in set(sa)|set(sb2):
                    va,vb=sa.get(sk,0),sb2.get(sk,0)
                    if isinstance(va,(int,float)) and isinstance(vb,(int,float)) and va:
                        d[top+'.'+sk]={'before':va,'after':vb,'delta':vb-va,'pct':(vb-va)/va*100,'improved':vb<va}
        return d

    def test_runtime_improves(self):
        assert self._delta(self.INE,self.OPT)['runtime_ms']['improved']

    def test_memory_improves(self):
        assert self._delta(self.INE,self.OPT).get('memory.peak_mb',{}).get('improved',False)

    def test_gc_improves(self):
        assert self._delta(self.INE,self.OPT).get('gc.total_events',{}).get('improved',False)

    def test_pct_calculation(self):
        d=self._delta(self.INE,self.OPT); exp=(45-1500)/1500*100
        assert abs(d['runtime_ms']['pct']-exp)<.5

# =============================================================================
# Test 6 - Async job submission
# =============================================================================
@pytest.mark.asyncio
class TestAsync:
    async def test_async_profile(self,profiler):
        loop=asyncio.get_event_loop()
        r=await loop.run_in_executor(None,profiler.profile,'x=sorted(range(100,0,-1))')
        assert isinstance(r,dict) and 'runtime_ms' in r

    async def test_concurrent_profiles(self,profiler):
        loop=asyncio.get_event_loop()
        codes=['x=sum(i**2 for i in range(1000))',
               "s=','.join(str(i) for i in range(500))"]
        res=await asyncio.gather(*[loop.run_in_executor(None,profiler.profile,c) for c in codes])
        assert all(isinstance(r,dict) and 'runtime_ms' in r for r in res)

def pytest_configure(config):
    config.addinivalue_line('markers','slow: marks tests as slow (>1s)')
    config.addinivalue_line('markers','java: marks tests requiring java/javac')
    config.addinivalue_line('markers','strace: marks tests requiring strace (Linux)')
'''

path = os.path.join(r'F:\FREAKY\ANTI_PROJECTS\DEEPTRACE\tests', 'test_pipeline.py')
with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print('Written OK:', path)

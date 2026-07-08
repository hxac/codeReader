# 运行时配置查找与就近匹配回退

## 1. 本讲目标

本讲承接 [u8-l2 持久化调优配置生成器 CLI](u8-l2-persistent-autotune-cli-generator.md)：那篇讲的是**如何离线生成**一张「设备 JSON」(逐形状 benchmark 出最优 launch config 并落盘)，本讲讲的是**运行时如何用上**这张 JSON。

学完后你应当能够：

1. 说清一次 `ffpa_attn_func` 调用在 `autotune=False`(默认) 时，是怎样从设备 JSON 里**查**出该用的 `BLOCK_M/BLOCK_N/num_warps/...` 的。
2. 掌握**过滤阶段**的硬过滤字段(direction / kernel / dtype / causal / has_attn_bias / has_dropout 等)，以及 dtype 与 TMA/WS 的**推断回退**规则。
3. 掌握**就近匹配阶段**两条不同规则：head_dim 用 `nearest_value`(最近、并列取大)，seqlen 用 `upper_or_max_value`(最小上界、否则取最大)。
4. 会用 `FFPA_TUNED_CONFIG_DIR` / `FFPA_SKIP_PERSISIT_TUNED_CONFIG` / `FFPA_LOGGER_LEVEL` 三个环境变量做调试与对照实验。

## 2. 前置知识

- **launch config**：Triton kernel 的启动参数集合，主要是 tile 大小(`BLOCK_M`/`BLOCK_N`/`BLOCK_HEADDIM_*`)与线程组织(`num_warps`/`num_stages`/`num_ctas`/`maxnreg`)。同一份 kernel 代码，不同 config 性能差很多，这就是要「调优」的原因。
- **持久化配置(device JSON)**：把某张显卡上对各种 (head_dim, seqlen, dtype, causal, ...) 形状调出的最优 config，按一个固定 schema 写成一个 JSON 文件，文件名由显卡名派生(如 `NVIDIA_L20.json`)。详见 u8-l2。
- **运行时查找(lookup)**：默认 `autotune=False` 时，Triton 后端不会在线 benchmark，而是去设备 JSON 里**查**一个匹配形状的 config；查不到就回退到代码里写死的**默认 config**。
- **就地匹配 vs 就近匹配**：若运行时形状恰好等于某个已调优形状，称「就地命中」；若不等于(例如运行时 D=384 但只调过 D=320/512)，就要用启发式规则选一个**最接近**的，称「就近匹配」。
- 关键术语：方向(direction = forward/backward)、kernel 名(`fwd_generic`/`decode_fwd_stage1`/`bwd_generic`/...)、schema_version、compute_capability、lru_cache。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`src/ffpa_attn/triton/_persistent_autotune.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py) | 本讲主角。定义 `PersistentConfigRequest`、文件加载、过滤、就近匹配与对外入口 `lookup_persistent_config`。 |
| [`src/ffpa_attn/triton/_ffpa_fwd.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py) | Triton 前向 launcher。在 `autotune=False` 分支里构造 request 并调用 `lookup_persistent_config`，查不到则用默认 config。 |
| [`src/ffpa_attn/triton/_ffpa_bwd.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py) | Triton 反向 launcher，同样在多处调用 lookup(主反向 / preprocess / decode 反向 / dKdV / dQ)。 |
| [`src/ffpa_attn/logger.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py) | `debug_once` 去重日志与 `FFPA_LOGGER_LEVEL`，是观察「是否命中持久化配置」的窗口。 |
| [`docs/user_guide/autotune.md`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/user_guide/autotune.md) | 官方用户指南，含匹配规则示例表与环境变量用法。 |
| [`tests/test_persistent_autotune_config.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py) | 查找逻辑的单元测试，是理解边界行为的最佳活教材。 |

## 4. 核心概念与源码讲解

### 4.1 整体定位：查找发生在哪、返回 None 意味着什么

#### 4.1.1 概念说明

持久化配置查找是「在线 autotune」与「写死默认 config」之间的**第三条路**：它不在进程内 benchmark(省启动时间)，而是读一张预先调好的 JSON，按运行时形状挑一个 config。因此它必须**极快且永不抛异常**——任何查不到的情况都返回 `None`，由调用方回退到默认 config，绝不让训练因「没有 JSON」而中断。

理解这条链路的关键是三态优先级(由前向 launcher 体现)：

```text
autotune=True  →  Triton 在线 autotune(进程内 benchmark，不读 JSON)
autotune=False →  lookup_persistent_config(request)
                   ├─ 命中  → 用 JSON 里的 config
                   └─ None  → 用代码里写死的默认 config
```

#### 4.1.2 核心流程

以前向 generic 路径为例：

1. launcher 进入 `else`(非 autotune)分支。
2. 用当前形状构造一个 `PersistentConfigRequest`。
3. 调 `lookup_persistent_config(request)`。
4. `persisted_config or {默认config}`：命中用之，否则用默认。

#### 4.1.3 源码精读

前向 generic launcher 的非 autotune 分支([`_ffpa_fwd.py`:L958-L983](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L958-L983))：

```python
else:
  persisted_config = lookup_persistent_config(
    PersistentConfigRequest(
      direction="forward",
      kernel="fwd_generic",
      autotune_mode=autotune_mode,
      dtype=dtype_name(q.dtype),
      headdim=headdim,
      seqlen_q=seqlen_q,
      seqlen_k=seqlen_k,
      causal=causal,
      has_attn_bias=has_attn_bias,
      has_dropout=has_dropout,
      nheads_q=nheads_q,
      nheads_kv=nheads_kv,
      device_index=q.device.index,
    )
  )
  launch_config = persisted_config or {
    "BLOCK_M": 128, "BLOCK_N": 64,
    "BLOCK_HEADDIM_QK": 64, "BLOCK_HEADDIM_V": 64,
    "num_warps": 8, "num_stages": 3,
  }
```

注意三点：① request 把「我是谁、什么形状、什么变体」全部打包成一个不可变(`@dataclass(frozen=True)`)信封(定义见 [`_persistent_autotune.py`:L166-L214](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L166-L214))；② `device_index=q.device.index` 显式带上设备号，避免重复 cache 命中时再查一次当前设备；③ `or {默认}` 就是「None 即回退」的落点。decode stage1 路径结构相同，只是 `kernel="decode_fwd_stage1"` 且多带 `use_gemv`([`_ffpa_fwd.py`:L1171-L1188](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1171-L1188))。

#### 4.1.4 代码实践

**实践目标**：确认「无 JSON 时静默回退默认 config、不报错」。

1. 在无 GPU 或无 JSON 的环境里读 launcher 源码，定位 `persisted_config or {...}` 这一行。
2. 想象 `lookup_persistent_config` 返回 `None`，回答：`launch_config` 会是什么？
3. **预期结果**：`launch_config` 取花括号里的默认 dict，kernel 照常启动，不抛异常。**待本地验证**：在装了 FFPA 但没有设备 JSON 的机器上跑一次前向，应正常出结果(只是用默认 config)。

#### 4.1.5 小练习与答案

- **练习**：为什么 request 设计成 `frozen=True`？  
  **答案**：因为它要作为 `lru_cache` 的 key(见 4.4)，可变对象不能可靠地做字典 key；冻结后同一形状的 request 必然哈希相等，保证缓存命中。
- **练习**：如果 `device_index` 不传会怎样？  
  **答案**：cache key 函数会改调 `torch.cuda.current_device()` 兜底(见 [`_persistent_autotune.py`:L530-L536](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L530-L536))，多一次设备查询，功能不变但略慢。

---

### 4.2 加载链：设备名优先、架构号回退

#### 4.2.1 概念说明

查找的第一步是「把 JSON 文件读进内存成 entries 列表」。这里有一个两级回退设计：

- **第一级(设备名匹配)**：用 `torch.cuda.get_device_name()` 得到显卡名，做文件名安全化(空格/特殊字符→下划线)后找 `{name}.json`。
- **第二级(架构号回退)**：若设备名文件不存在，扫描目录下所有 JSON，按文件内的 `compute_capability` 字段找第一个与本机 `major.minor` 匹配的。

这样，即便没为某张具体显卡单独调优，只要有一张**同架构**的 JSON，也能复用——代价是性能可能不是最优。

#### 4.2.2 核心流程

```text
load_config_entries(device_name)
  ├─ path = device_config_path(...)         # {sanitized_name}.json
  ├─ 候选 = [path, path.with_name("{stem}.config.json")]  # 兼容旧命名
  ├─ 逐个读：schema_version 必须 == SCHEMA_VERSION(=1)，否则视为空
  └─ 命中 → 缓存到 _CONFIG_CACHE 并返回 entries
若 entries 为空：
  _load_arch_config_entries(config_dir, device_index)
  ├─ arch = get_device_properties → "major.minor"
  ├─ 排序扫描目录下所有 .json
  ├─ 跳过 schema 不符 / compute_capability != arch 的文件
  └─ 取第一个匹配文件的 entries，缓存到 _ARCH_CONFIG_CACHE
```

两级缓存(`_CONFIG_CACHE`、`_ARCH_CONFIG_CACHE`)保证同一进程内重复查找不重复读盘。设备名本身也缓存在 `_DEVICE_NAME_CACHE`。

#### 4.2.3 源码精读

设备名安全化与路径([`_persistent_autotune.py`:L236-L257](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L236-L257))：

```python
def sanitize_device_name(device_name: str) -> str:
  stem = re.sub(r"[^0-9A-Za-z]+", "_", device_name.strip()).strip("_")
  return stem or "unknown_device"
```

所以 `"NVIDIA L20"` → `NVIDIA_L20`，`"NVIDIA GeForce RTX 5090"` → `NVIDIA_GeForce_RTX_5090`(与仓库里实际存在的 [`configs/NVIDIA_L20.json`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/configs/NVIDIA_L20.json)、[`configs/NVIDIA_GeForce_RTX_5090.json`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/configs/NVIDIA_GeForce_RTX_5090.json) 文件名一致)。

设备名优先加载([`_persistent_autotune.py`:L409-L444](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L409-L444))，关键点是用 `try/except` 兜住所有 IO/JSON 异常返回空列表，并校验 `schema_version`：

```python
if int(payload.get("schema_version", -1)) != SCHEMA_VERSION:
  entries = []
```

架构号回退([`_persistent_autotune.py`:L447-L492](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L447-L492))，核心是逐文件比对 `payload.get("compute_capability") != arch` 则 `continue`。注意它是**首个匹配即返回**，目录里文件排序后顺序决定取哪个，因此多张同架构卡混放时行为依赖文件名排序——生产上应优先为每张卡生成专属 JSON。

#### 4.2.4 代码实践

**实践目标**：验证「设备名优先、架构号回退、二者皆无则空」。

1. 读测试 `test_device_specific_skips_arch_fallback`([`test_persistent_autotune_config.py`:L1614-L1686](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L1614-L1686))：它同时写了一个设备名文件(BLOCK_M=128)和一个同架构(8.9)的冲突文件(BLOCK_M=64)。
2. **预期结果**：lookup 返回 BLOCK_M=128，证明设备名文件**压制**架构回退。
3. 再读 `test_arch_fallback_skips_wrong_arch`([同文件:L1689-L1750](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L1689-L1750))：当本机 arch=12.0、文件标 8.9 时返回 `None`，证明架构号必须严格相等。

#### 4.2.5 小练习与答案

- **练习**：为什么 `load_config_entries` 要同时尝试 `{stem}.json` 和 `{stem}.config.json` 两个文件名？  
  **答案**：兼容历史命名约定(`.config.json` 后缀)，保证旧产物仍可被读到。
- **练习**：schema_version 不符时为何直接当空而非抛异常？  
  **答案**：查找链路承诺「永不中断训练」；schema 升级后旧 JSON 视为不可用、回退默认 config，比报错更安全。

---

### 4.3 过滤阶段：把 entries 收敛到候选集

#### 4.3.1 概念说明

拿到 entries 列表后，下一步是**过滤**：只保留与本次 request 语义兼容的 entry。过滤字段分两类：

- **硬过滤(必须相等，否则 `continue`)**：`direction`、`kernel`、`causal`、`preprocess_d_chunk`、`bias_grad`、`grad_kv_storage_dtype`、`use_gemv`、`has_attn_bias`、`has_dropout`。
- **带推断回退的字段**：`dtype`(fp16 可回退到 bf16 条目)、`enable_tma`/`enable_ws`(老 JSON 没有这俩字段时，从 kernel 名与 `warp_specialize` 推断)。

一个反直觉但重要的设计：**head 布局(nheads_q/nheads_kv)不参与硬过滤**，只作为「平局偏好」。原因是 batch/头数随 workload 频繁变化，若要求头数严格相等会大量 miss；而 tile config 对头数不敏感，故允许复用。

#### 4.3.2 核心流程

```text
for entry in entries:
  if direction/kernel 不匹配: continue
  if dtype == request.dtype:        放 exact_candidates
  elif request.dtype=="fp16" 且 entry.dtype=="bf16":  放 fallback_candidates   # fp16→bf16 回退
  else: continue                                                              # bf16 不回退 fp16
  逐项硬过滤(causal/preprocess/bias_grad/grad_kv/use_gemv/has_attn_bias/has_dropout)
  TMA/WS 推断回退(老 JSON 用 kernel 名 + warp_specialize 推断)
  通过 → append 到对应候选集
candidates = exact_candidates 若非空 else fallback_candidates
```

#### 4.3.3 源码精读

dtype 的「精确优先、fp16 回退 bf16」([`_persistent_autotune.py`:L611-L617](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L611-L617))：

```python
if entry_dtype == request.dtype:
  target = exact_candidates
elif request.dtype == "fp16" and entry_dtype == "bf16":
  target = fallback_candidates
else:
  continue
```

方向是不对称的：fp16 查找时若无 fp16 条目可借用 bf16 条目(二者在 Hopper/Ampere 上 tile 选择通常接近)，但 bf16 查找**绝不**借用 fp16 条目(bf16 的数值范围/精度特性不同，tile 偏好可能不一样)。最终在 [`L689-L691`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L689-L691) `exact_candidates if exact_candidates else fallback_candidates` 体现「精确压倒回退」。

TMA/WS 的推断回退针对**老 JSON**(没有 `enable_tma`/`enable_ws` 字段)。新 JSON 会显式记录这俩 flag，避免「同形状的 TMA-only 与 TMA+WS 两次调优互相复用」。推断逻辑([`_persistent_autotune.py`:L656-L679](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L656-L679))：

```python
sm90_tma_kernels = {"fwd_sm90_generic", "bwd_sm90_generic", ...}
inferred_enable_tma = request.kernel in sm90_tma_kernels
inferred_enable_ws = bool(config.get("warp_specialize", False)) if inferred_enable_tma else False
```

通用辅助 `_entry_flag_matches`([`L539-L552`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L539-L552))表达三档语义：request 未声明该 flag(`expected is None`)则一律通过；entry 有该字段则要求严格相等；entry 没有该字段则用推断值比对。

head 布局不参与过滤、只做平局偏好，由 `_head_layout_rank`([`L503-L527`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L503-L527))实现：精确匹配头布局返回 0、否则返回 1，仅在最后多个候选并列时用它选最优(见 4.4 末尾)。

#### 4.3.4 代码实践

**实践目标**：用测试看清「mask/dropout 是硬过滤、头布局不是」。

1. 读 `test_lookup_filters_mask_dropout_but_not_head_layout`([`test_persistent_autotune_config.py`:L1112-L1259](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L1112-L1259))。该测试在 JSON 里放了 5 个 entry：基线、`has_attn_bias=True`、`has_dropout=True`、`nheads_kv=2`、`nheads_kv=1`(后两个头布局不同)。
2. 用 `has_attn_bias=False, has_dropout=False, nheads_q=8, nheads_kv=8` 查 → 命中基线(BLOCK_M=64)。
3. 改 `has_attn_bias=True` → 命中 bias 专用 entry(BLOCK_M=128)，证明 bias 是硬过滤。
4. 改 `nheads_kv=1` → 仍命中(MQA 的 entry，BLOCK_HEADDIM_QK=128)，但注意这是**因为它有独立 entry**；若没有，会落到基线，证明头布局不挡路。
5. **预期结果**：与测试断言逐一对应。**待本地验证**：`pytest tests/test_persistent_autotune_config.py::test_lookup_filters_mask_dropout_but_not_head_layout -q`。

#### 4.3.5 小练习与答案

- **练习**：runtime 用 fp16、但 JSON 里只有 bf16 条目，会命中吗？反过来呢？  
  **答案**：fp16→可命中 bf16(走 fallback_candidates)；bf16→不会命中 fp16(直接 `continue`)，见 `test_bf16_does_not_fallback_to_fp16`([同文件:L1420-L1462](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L1420-L1462))。
- **练习**：为什么 head 布局只做「偏好」不做「过滤」？  
  **答案**：tile config(BLOCK_M/N、num_warps)对头数不敏感，但 batch/头数因 workload 而异；强制相等会导致大量 miss，反而退化到默认 config。

---

### 4.4 就近匹配：nearest_value 与 upper_or_max_value

> 本模块对应规格里要求的「`lookup_persistent_config`/`_lookup_persistent_config_cached`」主入口，以及两个匹配原语。为可读性，4.3 单讲了过滤；这里讲过滤之后的「选一个」与对外入口的全貌。

#### 4.4.1 概念说明

过滤后通常仍有多个候选(不同 head_dim、不同 seqlen 的 entry)。要最终选一个，对**两个维度用不同规则**：

- **head_dim**：取**最近**的 persisted 值，并列(等距)时取**较大**者。
- **seqlen_q / seqlen_k**：取**最小的、≥运行时值的** persisted 值；若运行时值比所有 persisted 都大，则取**最大**的 persisted 值。

为何规则不同？直观上：head_dim 连续地改变寄存器/SRAM 占用与 tile 有效性，**最近**的形状是最相似的代理；而 seqlen 不改变 tile 本身的有效性(grid 由运行时 seqlen 现算，任何 persisted tile config 在功能上都成立)，选择只是性能启发，取**上界**避免复用一个为更短序列调优、可能未充分压榨长序列并行度的 config。

#### 4.4.2 核心流程

```text
# head_dim: nearest_value(并列取大)
headdim_target = nearest_value(所有候选 headdim 集合, request.headdim)
candidates = [e for e in candidates if e.headdim == headdim_target]

# seqlen_q: upper_or_max_value(最小上界，否则最大)
seqlen_q_target = upper_or_max_value(候选 seqlen_q 集合, request.seqlen_q)
candidates = [e for e in candidates if e.seqlen_q == seqlen_q_target]

# seqlen_k 同理(仅当 request.seqlen_k is not None)
seqlen_k_target = upper_or_max_value(...)

# 仍多个 → 按 head 布局偏好(_head_layout_rank)取最小
selected = min(candidates, key=_head_layout_rank)
return sanitize_kernel_config(kernel, selected["config"])
```

#### 4.4.3 源码精读

两个原语都很短。`nearest_value`([`_persistent_autotune.py`:L308-L320](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L308-L320))：

```python
def nearest_value(values, target):
  if not values:
    return None
  return min(sorted(values), key=lambda v: (abs(v - target), -v))
```

排序 key 是 `(距离, -值)`：第一关键字按距离升序(最近优先)，第二关键字 `-v` 升序即 `v` 降序——这就是「并列取大」的来源。例如 persisted `{320, 512, 640}`，target=384：\(|384-320|=64\)、\(|384-512|=128\)，选 320；target=448：\(|448-320|=128\)、\(|448-512|=64\)，选 512。

`upper_or_max_value`([`L323-L336`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L323-L336))：

```python
def upper_or_max_value(values, target):
  if not values:
    return None
  ordered = sorted(values)
  for value in ordered:
    if value >= target:
      return value
  return ordered[-1]
```

即「第一个 ≥ target 的」；都不满足则取末尾最大值。例如 persisted `{512,1024,2048,4096,8192}`，target=3000 → 4096；target=32768 → 8192(回退最大)。

主匹配逻辑([`L693-L732`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L693-L732))：先 head_dim 收窄、再 seqlen_q 收窄、再 seqlen_k 收窄，最后 `min(..., key=_head_layout_rank)` 并 `sanitize_kernel_config` 过滤掉该 kernel 不允许的 config key(白名单见 [`_KERNEL_CONFIG_KEYS`:L36-L163](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L36-L163))。

对外入口 `lookup_persistent_config`([`L735-L765`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L735-L765))做三件事：① 若 `FFPA_SKIP_PERSISIT_TUNED_CONFIG=1` 直接返回 None；② 把 request 转成 cache key 调 `_lookup_persistent_config_cached`(被 `@lru_cache` 包裹，见 [`L589-L594`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L589-L594))；③ DEBUG 模式下对比 `cache_info().hits` 区分「缓存命中」「首次选中」「未命中」三种日志。

#### 4.4.4 代码实践

**实践目标**：复现 docs 给的经典就近表，理解 D=384→320。

1. 读 `test_lookup_forward_uses_shape_grid_nearest`([`test_persistent_autotune_config.py`:L41-L104](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L41-L104))。JSON 里有两个 forward entry：(D=320, Nq=4096, Nkv=8192) 与 (D=512, Nq=2048, Nkv=8192)。
2. 用 `headdim=384, seqlen_q=3000, seqlen_k=32768` 查找。
3. 推演匹配过程：
   - head_dim：候选 {320, 512}，target=384 → \(\min(|384-320|,|384-512|)=\min(64,128)\) → **320**。剩下 (D=320) 一个候选。
   - seqlen_q：候选 {4096}，target=3000 → 最小 ≥3000 的是 **4096**。仍命中。
   - seqlen_k：候选 {8192}，target=32768 → 全部 < 32768，回退最大 **8192**。仍命中。
   - 最终选中 D=320 那条，返回其 config(BLOCK_M=64)。
4. **预期结果**：`config["BLOCK_M"] == 64`，与测试断言一致。这就是「运行时 D=384 就近匹配到已持久化 D=320」的完整解释。
5. 对照 docs 的表([`autotune.md`:L291-L307](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/user_guide/autotune.md#L291-L307))：D 384→320 / 448→512 / 900→1024；seqlen 3000→4096 / 32768→8192。**待本地验证**。

#### 4.4.5 小练习与答案

- **练习**：persisted head_dim 集合是 `{320, 512, 640, 768, 1024}`，runtime D=576 会匹配到哪个？  
  **答案**：\(|576-512|=64\)、\(|576-640|=64\)，等距并列取大 → **640**。
- **练习**：runtime seqlen=7000、persisted `{1024,2048,4096,8192}`，匹配哪个？  
  **答案**：最小 ≥7000 的是 **8192**。
- **练习**：为什么 head_dim 用「最近」而 seqlen 用「上界」？  
  **答案**：head_dim 直接影响 tile 有效性与寄存器占用，最近形状最相似；seqlen 不影响 tile 功能正确性(grid 现算)，选择纯属性能启发，取上界避免复用为更短序列调优的 config。

---

### 4.5 环境变量与 DEBUG 调试日志

#### 4.5.1 概念说明

三个环境变量控制查找行为与可观测性：

| 变量 | 作用 | 取值 |
| --- | --- | --- |
| `FFPA_TUNED_CONFIG_DIR` | 覆盖配置目录(默认包内 `configs/`) | 任意目录路径 |
| `FFPA_SKIP_PERSISIT_TUNED_CONFIG` | 强制跳过持久化查找(直接回退默认 config) | `1` 跳过；其他不跳过 |
| `FFPA_LOGGER_LEVEL` | FFPA 包日志级别 | `DEBUG`/`INFO`/... |

> 注意：`FFPA_SKIP_PERSISIT_TUNED_CONFIG` 是项目里**真实存在的拼写**(把 PERSIST 写成了 PERSISIT)，源码常量见 [`_persistent_autotune.py`:L25](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L25)。使用时必须照此拼写，否则无效。

DEBUG 日志用 `debug_once` 语义：同一行「缓存命中/选中/未命中」每进程只打一次，避免每次 attention 调用刷屏。

#### 4.5.2 核心流程

```text
lookup_persistent_config(request):
  if FFPA_SKIP_PERSISIT_TUNED_CONFIG == "1":  # 强制 miss
      DEBUG: "Persistent autotune lookup skipped by env"
      return None
  cache_key = (FFPA_TUNED_CONFIG_DIR, device_index, request)   # 目录也进 key
  if DEBUG 关闭: 直接返回 cached(*key)
  else: 调用前后比较 lru_cache.cache_info().hits：
        hits 增加 → "Persistent autotune cache hit"
        选中      → "Persistent autotune selected config"
        None      → "Persistent autotune lookup miss"
```

#### 4.5.3 源码精读

`FFPA_TUNED_CONFIG_DIR` 进入目录解析与 cache key 两条路径：[`runtime_config_dir`:L225-L233](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L225-L233) 读它决定目录；[`_lookup_cache_key`:L530-L536](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L530-L536) 把它的值塞进 key——所以**改这个变量后无需清缓存也能换目录**(key 变了自然重新查)。

跳过开关([`skip_persistent_tuned_config_from_env`:L354-L359](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L354-L359))：仅当值**严格等于 `"1"`** 才跳过。`lookup_persistent_config` 在最开头检查它([`L743-L748`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L743-L748))，意味着即便有完美匹配的 JSON 也会被强制忽略，便于对照「有/无持久化配置」的性能差异。

DEBUG 日志由 `_debug_lookup_message`([`L555-L586`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L555-L586))经 `logger.debug_once` 发出，打印 direction/kernel/mode/dtype/D/Nq/Nkv/causal/.../config。`debug_once` 实现在 [`logger.py`:L150-L153](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L150-L153) 与 [`L163`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L163)，去重 key 是 `(logger.name, level, 渲染后文本)`([`_log_once`:L125-L141](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L125-L141))。日志级别由 `FFPA_LOGGER_LEVEL` 决定([`_log_level_from_env`:L27-L34](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/logger.py#L27-L34))，默认 `INFO`(DEBUG 不显)。

#### 4.5.4 代码实践

**实践目标**：用 DEBUG 日志确认运行时命中持久化配置，并对照「跳过」前后的 config 来源。这是本讲的主实践。

1. 先用最小成本生成一张 smoke 配置(仅 4 个形状，见 u8-l2)：

   ```bash
   CUDA_VISIBLE_DEVICES=0 \
   FFPA_AUTOTUNE_MAX_CONFIGS=4 \
   python -m ffpa_attn.autotune \
     --mode fast --directions both \
     --overwrite --output-dir /tmp/ffpa-config-smoke
   ```

2. 在该目录下跑一次前向，开 DEBUG 并指向该目录：

   ```bash
   CUDA_VISIBLE_DEVICES=0 \
   FFPA_LOGGER_LEVEL=DEBUG \
   FFPA_TUNED_CONFIG_DIR=/tmp/ffpa-config-smoke \
   python -m ffpa_attn.bench --fwd-backend triton --no-bwd
   ```

3. **需要观察的现象**：日志里出现以 `Persistent autotune selected config` 或 `Persistent autotune cache hit` 开头的行，且 `config={...}` 里是 JSON 里的 tile 参数。
4. **预期结果**：第一次某形状查找打 `selected config`，之后同形状打 `cache hit`；二者都说明命中了持久化配置而非默认 config。
5. **对照实验**：再设 `FFPA_SKIP_PERSISIT_TUNED_CONFIG=1` 重跑，应看到 `Persistent autotune lookup skipped by env` 且后续不再有 selected/hit 行(回退默认 config)。**待本地验证**(依赖真实 GPU 与编译环境)。

> 若无 GPU，可退化为「源码阅读型实践」：读 `test_lookup_debug_logs_selected_config_cached_hit_and_miss`([`test_persistent_autotune_config.py`:L471-L543](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L471-L543))，它 monkeypatch 了 `debug_once` 捕获消息，断言第一次为 `selected config`、第二次为 `cache hit`，正是不开 GPU 也能验证日志三态的活教材。

#### 4.5.5 小练习与答案

- **练习**：把 `FFPA_TUNED_CONFIG_DIR` 从 A 改到 B 后，要不要清缓存？  
  **答案**：不用。`_lookup_cache_key` 把该变量值纳入 key([`L536`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L536))，目录变了 key 就变，自动重新查。
- **练习**：`FFPA_SKIP_PERSISIT_TUNED_CONFIG=true` 会不会触发跳过？  
  **答案**：不会。代码严格要求 `== "1"`([`L359`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L359))，`true`/`yes` 都不生效——这是与 logger 的 `_truthy_env` 不同的、更严格的约定。

## 5. 综合实践

把本讲四件事(加载、过滤、就近匹配、调试)串起来，做一次「端到端追踪」：

**任务**：解释一次具体调用 `ffpa_attn_func(q,k,v)` 在默认(非 autotune、triton 后端)路径下，是如何从 `NVIDIA_L20.json` 里选出 config 的，其中 q 为 `[1, 32, 3000, 384]` 的 bf16 张量、k/v 为 `[1, 32, 32768, 384]`、非 causal、无 mask/dropout。

要求按以下顺序作答(可对照 4.1–4.5)：

1. **加载**：当前设备名 → 文件名；若该文件缺失会走架构回退吗？此时 compute_capability 是多少(L20 = 8.9)？
2. **构造 request**：写出关键字段(direction/kernel/autotune_mode/dtype/headdim/seqlen_q/seqlen_k/causal/has_attn_bias/has_dropout)的取值。注意 `autotune_mode` 默认是 `"fast"`([`functional.py`:L195](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L195))，故只会命中 JSON 里 `autotune_mode=="fast"` 的 entry——而仓库自带的 `NVIDIA_L20.json` 顶层标注的是 `"max"`(见 [`configs/NVIDIA_L20.json`:L4](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/configs/NVIDIA_L20.json#L4))。请思考：单条 entry 自带 `autotune_mode` 字段吗？过滤用的是 entry 的字段还是文件顶层字段？(提示：过滤代码 [`L607-L609`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L607-L609) 没有读 `autotune_mode`——它由生成端保证「同 mode 不会混存」，运行时只信任文件顶层约定。这是一个值得在源码里核实的好问题。)
3. **就近匹配**：headdim 384 → ? ；seqlen_q 3000 → ? ；seqlen_k 32768 → ? 。给出每一步的候选集合与选值。
4. **调试**：用 `FFPA_LOGGER_LEVEL=DEBUG` 跑，预期看到哪两类日志行？

**参考要点**(请先自己作答再对照)：

1. 设备名 `NVIDIA L20` → `NVIDIA_L20.json`，存在则不走架构回退；L20 的 compute_capability = 8.9。
2. request 关键值：direction=`forward`、kernel=`fwd_generic`、autotune_mode=`fast`、dtype=`bf16`、headdim=`384`、seqlen_q=`3000`、seqlen_k=`32768`、causal=`False`、has_attn_bias=`False`、has_dropout=`False`。
3. 就近匹配：head_dim 候选 {320,512,640,768,1024} → **320**；seqlen_q 候选 {512,1024,2048,4096,8192,16384?}，最小 ≥3000 → **4096**；seqlen_k 同集合，32768 超过所有 → 回退最大(16384 若生成，否则 8192)。
4. 日志：首次 `Persistent autotune selected config: ... kernel=fwd_generic ...`，之后同形状 `Persistent autotune cache hit: ...`。

> 提示：第 2 步里那个 autotune_mode 过滤的「悬而未决」是故意留的探究点——去 [`L606-L690`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L606-L690) 自己核实查找循环是否比对 `autotune_mode`，并思考「生成端单 mode 落盘」这一不变量是如何让运行时省掉这项过滤的。docs 明确要求运行时 mode 与生成 mode 一致([`autotune.md`:L214](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/user_guide/autotune.md#L214))，机制由 `--output-dir` 分文件 + 文件顶层 `autotune_mode` 标注 + 用户/CI 部署约定共同保证。

## 6. 本讲小结

- 持久化查找是「在线 autotune」与「写死默认 config」之间的第三条路：`autotune=False` 时走 `lookup_persistent_config`，命中用 JSON config，未命中(`None`)回退默认 config，**永不抛异常**。
- 加载是**两级**：设备名文件优先(`{sanitized_name}.json`)，缺失时按 `compute_capability` 回退到同架构文件，二者皆空则空列表；schema_version 不符视为空。
- 过滤阶段硬过滤 direction/kernel/causal/has_attn_bias/has_dropout/use_gemv/bias_grad/grad_kv_storage_dtype 等；dtype **精确优先、fp16 可回退 bf16、bf16 不回退 fp16**；TMA/WS 对老 JSON 用 kernel 名 + `warp_specialize` **推断**；head 布局不参与过滤、只做并列偏好。
- 就近匹配两套规则：head_dim 用 `nearest_value`(最近、并列取大)，seqlen_q/k 用 `upper_or_max_value`(最小上界、否则最大)。
- 全程 `@lru_cache` 缓存，cache key 含 `FFPA_TUNED_CONFIG_DIR`、device_index 与 request 本身；`FFPA_SKIP_PERSISIT_TUNED_CONFIG=1`(注意拼写)强制跳过；`FFPA_LOGGER_LEVEL=DEBUG` 配合 `debug_once` 观察 selected/cache hit/miss 三态。

## 7. 下一步学习建议

- 下一篇 [u8-l4 Ray 多 GPU 并行调优](u8-l4-ray-multi-gpu-autotune.md) 讲「如何用多张卡并行生成」本讲消费的这张 JSON——生成端与查找端正好闭环。
- 若想验证查找正确性，跑 `pytest tests/test_persistent_autotune_config.py tests/test_triton_autotune_mode.py -q`(见 [u9-l2 持久化配置与自动调优模式测试](u9-l2-persistent-config-autotune-tests.md))。
- 进一步可阅读 [`autotune.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py) 的生成端，对照 `_record_entry`/`_build_payload` 写入的字段与 本讲 `_lookup_persistent_config_cached` 读取的字段，理解 schema 的「写读对称性」。

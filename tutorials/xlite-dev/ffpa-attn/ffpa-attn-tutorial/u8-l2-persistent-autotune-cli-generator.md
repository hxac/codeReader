# 持久化调优配置生成器 CLI

## 1. 本讲目标

本讲承接 u8-l1（Triton 在线自动调优机制），把视角从「**进程内、用完即丢**」的在线 autotune，切换到「**离线生成、随包发布、运行时直接复用**」的持久化调优配置（persistent tuned config）。

读完本讲，你应当能够：

- 说清「在线 autotune」与「持久化配置」的区别，以及为什么生产环境要用后者。
- 用 `python -m ffpa_attn.autotune` 的命令行参数精确控制「**调什么、调哪些方向、调哪些形状、调哪些变体**」。
- 画出任务网格（`headdim × seqlen × causal × 变体`）的生成逻辑，分清 prefill 与 decode 两条分支。
- 读懂生成出的设备 JSON 的整体结构，并能逐字段解释一条 `entry` 的含义。
- 说出输出文件名 `NVIDIA_L20.json` 这种命名是怎么由显卡名 sanitize 出来的。

## 2. 前置知识

### 2.1 在线 autotune vs 持久化配置（承接 u8-l1）

u8-l1 讲过：Triton 后端在 `TritonBackend(autotune=True, autotune_mode="fast"/"max")` 下，会在**第一次**遇到一个新形状时，把一组候选 config（`BLOCK_M/BLOCK_N/num_warps/num_stages` 等）逐个 benchmark、选出最快的，缓存在**当前进程**里。

这种在线方式有两个缺点：

1. **启动慢**：每个新形状首次调用要付出调优成本。对于推理服务，这会让首个请求延迟飙升。
2. **结果不持久**：进程一退出，选出来的 config 就丢了，下次还得重调。

持久化配置解决这两个问题：**在目标显卡上离线跑一遍调优，把结果写成一张「设备 JSON」提交进仓库；之后所有进程默认（`autotune=False`）直接查这张表，零调优启动、结果可复现。**

> 一句话区分：在线 autotune 是「**边跑边选**」，持久化配置是「**事先选好写下来，照着读**」。本讲讲的就是「事先选好写下来」这个生成器。

### 2.2 autotune key 与「精确 key」的差别（承接 u8-l1）

在线路径为了减少重复调优，会把序列长度做**分桶**（fast 模式按 1024 一桶、8192 封顶）。但离线生成器不能分桶——否则生成器声称「我为 2048 调过优」，其实 2048 被 1024 的桶截胡了。所以生成器在调优期间会切到「**精确 key**」模式，让网格里的每一个目标形状（512、1024、2048……）都各自独立 benchmark 一次。这个开关由上下文管理器 `exact_autotune_seqlen_keys()` 实现，详见 [_autotune_utils.py:100-107](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_autotune_utils.py#L100-L107)，u8-l1 已铺垫，本讲在 main 流程里会用到。

### 2.3 一个名词约定

- **entry**：JSON 里的一条记录，描述「**某一个 kernel 在某一个形状/变体下的最优 launch config**」。一张设备 JSON 由几百到上千条 entry 组成。
- **task**：生成器内部的一个「调优任务」，对应「要为某个形状 benchmark 一遍」。一个 task 调完通常产出 1 条以上 entry（反向还会额外产 `bwd_preproc` 等）。

## 3. 本讲源码地图

本讲只围绕一个文件加两个辅助文件：

| 文件 | 作用 |
| --- | --- |
| [src/ffpa_attn/autotune.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py) | **生成器主体**。`python -m ffpa_attn.autotune` 的入口，含 CLI 参数、任务网格、调优执行、JSON 组装。 |
| [src/ffpa_attn/triton/_persistent_autotune.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py) | **JSON 格式与 IO 辅助**。定义网格常量 `DEFAULT_HEADDIMS/DEFAULT_SEQLENS`、schema、序列化、文件命名、运行时查找（查找属 u8-l3）。 |
| [src/ffpa_attn/triton/_autotune_utils.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_autotune_utils.py) | **seqlen key 分桶**。本讲只用到「精确 key」上下文管理器。 |
| [docs/user_guide/autotune.md](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/user_guide/autotune.md) | **官方使用文档**。所有命令行示例与运行时查找规则的权威说明。 |

入口确认：模块文件末尾 `if __name__ == "__main__": raise SystemExit(main())`，见 [autotune.py:1150-1151](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L1150-L1151)，所以 `python -m ffpa_attn.autotune` 直接调用本文件的 `main()`。

---

## 4. 核心概念与源码讲解

### 4.1 CLI 入口与 `_parse_args` 参数体系

#### 4.1.1 概念说明

生成器是一个标准的 argparse 命令行工具。它的全部能力都暴露在 `_parse_args()` 里。理解这些参数，就理解了「**这个工具能控制什么**」。

参数可分为四组：

1. **搜索强度**：`--mode fast|max`——决定候选 config 的数量与 seqlen 分桶粒度（fast 少而粗、max 多而细），与运行时 `autotune_mode` 严格对应。
2. **覆盖范围**：`--directions forward|backward|both`、`--dtypes bf16,fp16`、`--full-tasks`——决定调哪些方向、哪些 dtype、要不要追加 mask/dropout/GQA/MQA 变体。
3. **形状基底**：`--B`（batch）、`--H`（query 头数）——网格里所有 task 共享这两个基底形状。
4. **高级/工程**：`--enable-*-tma`、`--enable-*-ws`、`--enable-bwd-split-launch`（Hopper TMA/warp-specialize/split-launch 路径）、`--overwrite`、`--output-dir`、`--num-gpus`、`--ray-address`（多卡并行，属 u8-l4）。

#### 4.1.2 核心流程

```text
python -m ffpa_attn.autotune [flags]
        │
        ▼
_parse_args()                  # argparse 解析
        │
        ├── _parse_dtypes("bf16,fp16")  # 自定义 type，逗号分隔 → [torch.bfloat16, torch.float16]
        │
        └── _resolve_directional_cli_flags(args)  # 把旧版全局 --enable-tma/--enable-ws
                                                   # 展开成 --enable-fwd-tma/--enable-bwd-tma 等
        ▼
返回 argparse.Namespace
```

其中 `_parse_dtypes` 是个自定义的 argparse `type` 函数，把字符串 `"bf16,fp16"` 解析成 `list[torch.dtype]`，并去重、校验只允许 `fp16/bf16`，见 [autotune.py:101-114](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L101-L114)。

`_resolve_directional_cli_flags` 处理「**旧版全局开关 → 新版方向开关**」的兼容映射，见 [autotune.py:117-127](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L117-L127)：

```python
if args.enable_tma:
    args.enable_fwd_tma = True
    args.enable_bwd_tma = True
if args.enable_ws:
    args.enable_fwd_ws = True
    args.enable_bwd_ws = True
```

即旧版 `--enable-tma` 等价于新版 `--enable-fwd-tma --enable-bwd-tma`。新版方向开关更精细：前向和反向可以各自独立决定要不要走 TMA 路径。

#### 4.1.3 源码精读

`_parse_args` 的 `--mode`、`--directions`、`--full-tasks` 三个核心参数，见 [autotune.py:881-908](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L881-L908)：`--mode` 限定 `fast/max` 二选一、默认 `fast`；`--directions` 默认 `both`；`--full-tasks` 是 `store_true` 开关，默认关。

基底形状与 TMA/工程参数见 [autotune.py:892-982](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L892-L982)，其中 `--B` 默认 1、`--H` 默认 32（对应 docs 里「默认 B=1, H=32」的约定）；`--enable-bwd-split-launch` 有两个别名 `--bwd-split-launch/--bwd-split`，见 [autotune.py:949-956](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L949-L956)；`--num-gpus`/`--ray-address` 是多卡并行入口（u8-l4 详讲），见 [autotune.py:968-982](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L968-L982)。

> 注意：`--mode` 必须与**运行时**的 `autotune_mode` 严格一致。生成时用 `--mode fast`，运行时就得是 `triton_autotune_mode="fast"`；用 `--mode max` 生成，运行时就得是 `"max"`。这是运行时查找（u8-l3）的硬过滤条件之一。

#### 4.1.4 代码实践

**实践目标**：熟悉 CLI 的全部开关，理解新旧 TMA/WS 开关的等价关系。

**操作步骤**：

1. 不带任何参数运行，查看帮助（本命令只打印帮助、不消耗 GPU，可立即在本地执行）：

   ```bash
   python -m ffpa_attn.autotune --help
   ```

2. 对照 [_parse_args](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L877-L983) 在帮助输出里找到：`--mode`、`--directions`、`--B`、`--H`、`--full-tasks`、`--dtypes`、`--enable-tma`、`--enable-fwd-tma`、`--overwrite`、`--output-dir`、`--num-gpus` 各自的默认值。

**需要观察的现象**：`--enable-tma` 的帮助文本里写的是「Compatibility alias for --enable-fwd-tma --enable-bwd-tma」，与 [_resolve_directional_cli_flags](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L117-L127) 的展开逻辑一致。

**预期结果**：帮助里能看到全部 ~16 个 flag，默认 `--mode fast`、`--directions both`、`--B 1`、`--H 32`、`--dtypes bf16`、`--full-tasks` 关闭。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--mode` 生成时选了 `fast`，运行时就也必须用 `fast`？

**参考答案**：运行时查找会把 `autotune_mode` 当作硬过滤条件（见 [docs/user_guide/autotune.md:214](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/user_guide/autotune.md#L214)「The runtime lookup requires the mode to match」）。fast 与 max 的候选集不同、seqlen 分桶粒度也不同，混用会让查到的 config 与实际调过的形状对不上。

**练习 2**：用户输入 `--dtypes fp16,bf16,fp16`，`_parse_dtypes` 会返回什么？

**参考答案**：返回 `[torch.float16, torch.bfloat16]`。函数在 [autotune.py:110-111](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L110-L111) 做了去重，重复的 `fp16` 只保留一次。

---

### 4.2 任务网格：`_iter_forward_tasks` / `_iter_backward_tasks` 与 prefill/decode 划分

#### 4.2.1 概念说明

「调优」本质上是对**一组形状**逐个 benchmark。这组形状就是**任务网格（task grid）**。网格由两个常量张成，定义在 [_persistent_autotune.py:29-30](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L29-L30)：

```python
DEFAULT_HEADDIMS = [320, 512, 640, 768, 1024]            # head_dim 维
DEFAULT_SEQLENS   = [1, 512, 1024, 2048, 4096, 8192, 16384]  # 序列长度维
```

注意 FFPA 只对**大 head_dim**（≥320，承接 u1-l1 的适用边界）做持久化调优——小 D 本来就走 aten/SDPA，没必要调。

序列长度网格里藏着一个关键设计：`1` 这个值**只用于 decode 的 query 长度**（`Nq=1`），不会被当作 KV 长度——因为「单 token 的 KV cache」不是有意义的 decode 基准（见 [docs/user_guide/autotune.md:230](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/user_guide/autotune.md#L230)）。

#### 4.2.2 核心流程

每个 task 用一个 frozen dataclass `TuneTask` 描述，见 [autotune.py:71-98](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L71-L98)。前向网格生成 `_iter_forward_tasks` 的逻辑：

```text
对每个 dtype in dtypes:
  对每个 headdim in DEFAULT_HEADDIMS:
    对每个 causal in (False, True):
      ── prefill 分支 ──
      对 seqlen_q in prefill_seqlens (>=512):
        对 seqlen_k in prefill_seqlens:
          若 causal 且 seqlen_k < seqlen_q: 跳过   # 因果掩码要求 Nkv>=Nq（承接 u2-l3）
          产出 task(forward, Nq=seqlen_q, Nkv=seqlen_k)
      ── decode 分支 ──
      对 seqlen_k in decode_kv_seqlens (prefill 里 >1 的):
        产出 task(forward, Nq=1, Nkv=seqlen_k, case_name="decode-attn")
  若 full_tasks: 追加 _iter_full_variant_tasks(...)   # 下一节讲
```

反向 `_iter_backward_tasks` 结构几乎相同（见 [autotune.py:292-346](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L292-L346)），唯一差别在 decode：反向用 `decode_query_seqlens = [1]`，即只调 `Nq=1` 的 decode 反向（与 u5-l3 的 decode 反向两阶段对应）。

一个**显存自适应**细节：`16384` 这个序列长度只有在显卡显存 ≥ 48 GiB 时才进入网格，否则被砍掉，由 `_available_seqlens()` 实现，见 [autotune.py:138-144](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L138-L144)：

```python
if total_memory >= 48 * 1024**3:
    return list(DEFAULT_SEQLENS)
return [value for value in DEFAULT_SEQLENS if value < 16384]
```

这样小显存显卡不会因为 `16384` 的形状 OOM 而整个生成失败。

#### 4.2.3 源码精读

prefill / decode 的切分点在 [_iter_forward_tasks](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L237-L289)：

```python
prefill_seqlens = [value for value in seqlens if value >= 512]
decode_kv_seqlens = [value for value in prefill_seqlens if value > 1]
```

前向的 prefill 双循环（含 causal 跳过）见 [autotune.py:249-264](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L249-L264)；decode 循环（固定 `Nq=1`）见 [autotune.py:265-278](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L265-L278)，`case_name="decode-attn"` 用于日志与 JSON 元数据。

网格规模感受：仅前向、单 dtype、单 headdim 下，prefill 有「非因果 6×6 + 因果三角」对（以含 16384 的 6 个 prefill 长度为例 ≈ 57 对），decode 有 `2×6=12`，合计约 69 个 task/headdim；乘以 5 个 headdim 与 dtype 数、再翻倍加反向——全网格轻松上千个 task，这正是为什么 `--full-tasks` 默认关、smoke 测试要用 `FFPA_AUTOTUNE_MAX_CONFIGS` 截断。

#### 4.2.4 代码实践

**实践目标**：理解 prefill/decode 切分与显存自适应，会估算网格规模。

**操作步骤**：

1. 阅读 [_iter_forward_tasks:243-278](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L243-L278)，确认 `prefill_seqlens` 与 `decode_kv_seqlens` 的取值。
2. 阅读确认：decode 分支里 `seqlen_q` 恒为 `1`，没有任何 task 把 `Nkv=1` 单独调优。
3. 在纸面推算：在一台 **16 GiB** 显存的显卡上，`_available_seqlens()` 返回的网格是什么？

**需要观察的现象**：16 GiB < 48 GiB，所以 `16384` 被过滤。

**预期结果**：`[1, 512, 1024, 2048, 4096, 8192]`（缺少 16384）。

#### 4.2.5 小练习与答案

**练习 1**：为什么前向 causal 分支里要 `if causal and seqlen_k < seqlen_q: continue`？

**参考答案**：因果掩码要求 query 行对齐 KV 尾部，需 `Nkv >= Nq`（承接 u2-l3 的尾对齐约定）。当 `seqlen_k < seqlen_q` 时该组合非法，跳过可避免无效调优。

**练习 2**：为什么 decode 的 KV 长度用 `decode_kv_seqlens`（即 `>1` 的 prefill 长度），而不直接用含 `1` 的 `seqlens`？

**参考答案**：单 token KV cache（`Nkv=1`）不是有意义的 decode 基准目标，docs 明确说明 decode 不生成 `Nkv=1` 用例（[autotune.md:230](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/user_guide/autotune.md#L230)）。

---

### 4.3 `--full-tasks` 变体：`_iter_full_variant_tasks` 与 GQA/MQA 头数解析

#### 4.3.1 概念说明

默认网格只覆盖「**无 mask、无 dropout、头数相等（MHA）**」的基线形状。但真实模型还会用到 attn_mask（位置偏置）、dropout、GQA、MQA，这些会让 kernel 走**不同的内部分支**（承接 u4-l4 / u2-l4），最优 config 也不同。

`--full-tasks` 开关就是为这些「**单特性规范变体**」补调优。注意它只加 4 种**各自单独**的变体，**不是笛卡尔积**——即有 `gqa` 的用例和 `dropout` 的用例分别调，不会生成「gqa+dropout」组合（[autotune.md:256-258](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/user_guide/autotune.md#L256-L258) 明确声明）。

#### 4.3.2 核心流程

`_iter_full_variant_tasks` 对「每个 prefill seqlen」产出一组变体 task，见 [autotune.py:161-234](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L161-L234)：

```text
对每个 seqlen in prefill_seqlens:
  产 task(has_attn_bias=True,  case_name="attn-mask")   # 紧凑可加偏置 [1,1,1,Nkv]
  产 task(has_dropout=True,    case_name="dropout")     # dropout_p=0.1
  若 1 < gqa_heads < heads:
      产 task(nheads_kv=gqa_heads, case_name="gqa")     # GQA
  若 heads > 1:
      产 task(nheads_kv=1,      case_name="mqa")        # MQA
```

其中 GQA 的 KV 头数不是硬编码，而是由 `_resolve_gqa_heads(heads)` 按「`heads//4`、向下找到能整除 heads 的最大值」算出，见 [autotune.py:147-158](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L147-L158)：

```python
candidate = max(1, num_heads // 4)
while candidate > 1 and num_heads % candidate != 0:
    candidate -= 1
return candidate
```

例如默认 `--H 32`：`32//4=8`，`32%8==0`，故 `gqa_heads=8`（即 group_size=4）。MQA 则固定 `nheads_kv=1`（group_size=32）。

变体如何进入调优？`_iter_forward_tasks` / `_iter_backward_tasks` 在每个 (dtype, headdim) 外层循环末尾，若 `full_tasks` 为真就 `tasks.extend(_iter_full_variant_tasks(...))`，见 [autotune.py:279-288](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L279-L288)。变体 task 在执行时，`_tune_forward`/`_tune_backward` 会据 `task.has_attn_bias`/`has_dropout`/`nheads_q != nheads_kv` 把这些参数真实地传给 `ffpa_attn_func`（见 [_tune_forward:529-546](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L529-L546)），从而让 kernel 真的走 mask/dropout/GQA 分支。

#### 4.3.3 源码精读

4 种变体的 `TuneTask` 构造见 [autotune.py:180-233](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L180-L233)。GQA 的守卫 `if 1 < gqa_heads < heads` 排除了两种退化情形：`gqa_heads==1`（就是 MQA，已有专门 mqa 变体）和 `gqa_heads==heads`（退化为 MHA，基线已覆盖）。MQA 的守卫 `if heads > 1` 避免单头时再生成无意义的 MQA。

变体在主循环里被计数：`main()` 用 `full_variant_count = sum(task.case_name in {...} for task in tasks)` 统计并写进启动日志，见 [autotune.py:1027-1041](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L1027-L1041)。

#### 4.3.4 代码实践

**实践目标**：掌握 4 种变体的形状与触发条件，能手算 GQA 头数。

**操作步骤**：

1. 阅读 [_resolve_gqa_heads](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L147-L158)，在纸面推算 `--H 16` 和 `--H 8` 时的 `gqa_heads`。
2. 阅读 [_iter_full_variant_tasks](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L161-L234)，确认 attn-mask 变体用的偏置形状是 `[1,1,1,Nkv]`（紧凑广播），与 u4-l4 讲的「attn_bias 不物化」一致。

**需要观察的现象**：

- `--H 16`：`16//4=4`，`16%4==0` → `gqa_heads=4`，满足 `1<4<16`，会生成 gqa 变体。
- `--H 8`：`8//4=2`，`8%2==0` → `gqa_heads=2`，满足 `1<2<8`，会生成 gqa 变体。

**预期结果**：上两项推算成立；attn-mask 偏置由 `_make_attn_bias` 生成，反向形状为 `[1,1,1,seqlen_k]` 且 `requires_grad_(True)`（反向要算 bias 梯度），见 [autotune.py:392-405](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L392-L405)。

#### 4.3.5 小练习与答案

**练习 1**：`--full-tasks` 会不会生成「GQA + dropout」的组合用例？

**参考答案**：不会。docs 明确这些是「single-feature canonical variants, not a full Cartesian product」（[autotune.md:256-258](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/user_guide/autotune.md#L256-L258)）。每个变体只翻转一个特性，组合爆炸会令调优时间不可接受。

**练习 2**：为什么 `gqa` 变体加了 `if 1 < gqa_heads < heads` 守卫？

**参考答案**：`gqa_heads==1` 就是 MQA（已被 mqa 变体覆盖），`gqa_heads==heads` 就是 MHA（已被基线覆盖），两者都无需重复生成 gqa 变体。

---

### 4.4 JSON schema 与输出：`_entry_base` / `_record_entry` / `device_config_path`

#### 4.4.1 概念说明

调优结果最终要落盘成一张「设备 JSON」。这张 JSON 的设计要同时满足三个约束：

1. **可被运行时高效查找**（u8-l3）：每条 entry 要带足够多的过滤字段，让运行时能精确命中。
2. **跨 FFPA 版本兼容**：带 `schema_version`，且旧 JSON 缺字段时按「无 mask、无 dropout」语义退化。
3. **设备隔离**：文件名按显卡名生成，不同显卡绝不混用（在一张卡上调的 config 不能当另一张卡的基线）。

#### 4.4.2 核心流程

整个落盘流程在 `main()` 尾部，见 [autotune.py:1113-1146](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L1113-L1146)：

```text
entries(dict)        # 调优过程中 _record_entry 逐条写入，key=tuple 去重
    │
    ▼
sorted(entries.values(), ...)   # 按固定字段排序，保证 JSON 稳定可 diff
    │
    ▼
_build_payload(...)             # 套上 top-level 元数据（schema_version、device、版本号、网格...）
    │
    ▼
write_config_file(payload, output_path, overwrite)
    │
    ▼
{output_dir}/{sanitized_device_name}.json
```

#### 4.4.3 源码精读

**（a）一条 entry 的字段——`_entry_base`**

`_entry_base` 是所有 entry 的公共字段构造器，见 [autotune.py:408-437](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L408-L437)。它产出的字段对照下表（取自真实 `NVIDIA_L20.json`）：

| 字段 | 含义 | 示例值 |
| --- | --- | --- |
| `direction` | `"forward"` / `"backward"` | `"forward"` |
| `kernel` | 调优的 kernel 名 | `"fwd_generic"`、`"decode_fwd_stage1"`、`"bwd_preproc"`、`"bwd_generic"`、`"decode_bwd_stage1"` 等 |
| `causal` | 是否因果 | `false` |
| `dtype` | 激活 dtype | `"bf16"` |
| `headdim` | head_dim | `320` |
| `seqlen_q` / `seqlen_k` | query / KV 序列长度（精确值） | `512` / `512` |
| `seqlen_q_bucket` / `seqlen_k_bucket` | 对应的 autotune key 桶值 | `512` / `512` |
| `nheads_q` / `nheads_kv` | query / KV 头数（仅元数据，查找不强求匹配） | `32` / `32` |
| `has_attn_bias` | 是否带可加偏置 | `false` |
| `has_dropout` | 是否带 dropout | `false` |
| `case_name` | 用例名（日志/元数据） | `"common"`、`"decode-attn"`、`"attn-mask"` |
| `config` | **最优 launch config**（见下） | `{"BLOCK_M":64,"BLOCK_N":64,...}` |
| `enable_tma` / `enable_ws` | 是否 SM90 TMA / warp-specialize 路径（仅 TMA entry 有） | `false` |

`config` 字段就是「最优 config」，由 `config_from_triton_config(wrapper.best_config)` 把 Triton 的 `Config` 对象序列化成纯 dict（剔除 `None` 与 `ir_override`），见 [_persistent_autotune.py:362-372](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L362-L372)。一个真实前向 config 长这样（取自 `NVIDIA_L20.json`）：

```json
"config": {
  "BLOCK_HEADDIM_QK": 128, "BLOCK_HEADDIM_V": 128,
  "BLOCK_M": 128, "BLOCK_N": 64,
  "num_ctas": 1, "num_stages": 3, "num_warps": 4
}
```

不同 kernel 的 entry 还会**额外**带专属字段（由 `_tune_forward`/`_tune_backward` 在 `_entry_base` 之后 `.update(...)`）：

- `bwd_preproc`：`preprocess_d_chunk`、`BLOCK_HEADDIM`（见 [autotune.py:671-681](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L671-L681)）。
- `bwd_generic` / `decode_bwd_stage1`：`bias_grad`、`grad_kv_storage_dtype`（见 [autotune.py:700-720](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L700-L720)）。
- `decode_*`：`use_gemv`（`Nq==1` 的单 query 特化，承接 u4-l3/u5-l3）。

**（b）entry 去重——`_record_entry`**

同一个 (kernel, 形状, 变体) 组合可能被调多次（如 forward 与 backward 共享某些形状、或 SM80 与 SM90 路径）。`_record_entry` 用一个**元组 key** 写进 dict 实现去重/覆盖，见 [autotune.py:440-466](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L440-L466)：

```python
key = (direction, kernel, causal, dtype, headdim, seqlen_q, seqlen_k,
       preprocess_d_chunk, bias_grad, grad_kv_storage_dtype, use_gemv,
       enable_tma, enable_ws)
if entry["kernel"] != "bwd_preproc":
    key += (nheads_q, nheads_kv, has_attn_bias, has_dropout)
entries[key] = entry   # 同 key 后写覆盖前写
```

注意 `bwd_preproc` 故意**不**把头数/mask/dropout 计入 key——因为预处理 kernel（delta 预处理，承接 u5-l1）与这些无关，对同一形状只保留一条即可。

**（c）top-level payload——`_build_payload`**

`_build_payload` 给 entries 套上一层设备/版本元数据，见 [autotune.py:833-874](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L833-L874)。关键字段：`schema_version`（来自 `SCHEMA_VERSION=1`，[_persistent_autotune.py:22](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L22)）、`device_name`/`device_name_sanitized`、`compute_capability`（如 `"8.9"`）、`autotune_mode`、`B`/`H`、`full_tasks`、`hardware_desc`（记录调优时开了哪些 TMA/WS/split-launch）、`tune_grid`（记录用了哪些 headdims/seqlens）、以及 `torch_version`/`triton_version`/`ffpa_version`/`generated_at`。

**（d）文件命名与输出目录——`device_config_path` / `sanitize_device_name`**

输出路径由 `device_config_path(output_dir)` 决定，见 [_persistent_autotune.py:246-257](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L246-L257)：它取当前显卡名、经 `sanitize_device_name` 转成文件名安全的形式、拼成 `{dir}/{name}.json`。`sanitize_device_name` 把所有非字母数字字符替换成 `_` 并去首尾下划线，见 [_persistent_autotune.py:236-243](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L236-L243)，所以：

```text
"NVIDIA L20"               -> NVIDIA_L20.json
"NVIDIA GeForce RTX 5090"  -> NVIDIA_GeForce_RTX_5090.json
```

默认目录是包内 `configs/`（由 `default_config_dir()` 返回 `.../triton/configs`，见 [_persistent_autotune.py:217-222](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L217-L222)）；`--output-dir` 或环境变量 `FFPA_TUNED_CONFIG_DIR` 可覆盖。仓库里现成就有两张：`NVIDIA_L20.json` 与 `NVIDIA_GeForce_RTX_5090.json`。

**（e）不覆盖保护——`write_config_file` 与 main 的双重检查**

`main()` 在调优前先做一次检查：若目标文件已存在且未传 `--overwrite`，直接 `return 0` 不重调，见 [autotune.py:1000-1002](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L1000-L1002)；最终写盘时 `write_config_file` 还会再校验一次，见 [_persistent_autotune.py:393-406](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L393-L406)。这种「双重不覆盖」是为了防止误把一张精心调好的设备 JSON 冲掉。

#### 4.4.4 代码实践

**实践目标**：跑一次最小生成，逐字段读懂一条 entry。

> 本实践需要一块 CUDA GPU（生成器启动即校验 `torch.cuda.is_available()`，见 [autotune.py:995-996](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L995-L996)）。下面的「步骤 1」需在本地 GPU 机器上执行（**待本地验证**）；若当前环境无 GPU，可直接做「步骤 2」的源码阅读型实践，用仓库自带的真实 JSON。

**操作步骤**：

1. **（需 GPU，待本地验证）** 用 `FFPA_AUTOTUNE_MAX_CONFIGS=4` 把网格截到前 4 个 task 做快速 smoke 生成：

   ```bash
   FFPA_AUTOTUNE_MAX_CONFIGS=4 \
   python -m ffpa_attn.autotune --mode fast --output-dir /tmp/ffpa-smoke
   ```

   截断由 `_limit_tasks` 实现（`max_configs_from_env()` 读环境变量），见 [autotune.py:349-353](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L349-L353)。生成完毕后打开 `/tmp/ffpa-smoke/{你的显卡名}.json`。

2. **（源码阅读型，无需 GPU）** 直接读仓库自带的真实设备配置，选一条 forward entry 逐字段解释：

   ```bash
   python -c "import json;d=json.load(open('src/ffpa_attn/triton/configs/NVIDIA_L20.json'));e=[x for x in d['entries'] if x['kernel']=='fwd_generic'][0];print(json.dumps(e,indent=2,sort_keys=True))"
   ```

**需要观察的现象**：这条 entry 的 `direction="forward"`、`kernel="fwd_generic"`、`config` 里含 `BLOCK_M/BLOCK_N/BLOCK_HEADDIM_QK/BLOCK_HEADDIM_V/num_warps/num_stages/num_ctas`，且**不含** `bias_grad`/`use_gemv`/`preprocess_d_chunk`（这些是反向/decode/预处理 entry 才有的专属字段）。

**预期结果**：你能对照本节 4.4.3(a) 的字段表，把这条 entry 的每一个字段都解释清楚；并发现 `seqlen_q == seqlen_q_bucket`（因为离线生成走「精确 key」，承接 2.2 节）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `bwd_preproc` 的 entry 在 `_record_entry` 里**不**把 `nheads_q/nheads_kv/has_attn_bias/has_dropout` 计入去重 key？

**参考答案**：delta 预处理 kernel（承接 u5-l1）只做 `rowsum(dO*O)`，与头数、mask、dropout 无关；对同一 (dtype, headdim, seqlen) 只需保留一条最优 config，否则会写出大量语义重复的 entry。代码用 `if entry["kernel"] != "bwd_preproc"` 把这四个字段排除在外（[autotune.py:459-465](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L459-L465)）。

**练习 2**：在某台 H200 上用 `--mode fast --enable-fwd-tma` 生成，运行时却用默认 `autotune=False` 且形状相同，运行时会命中 `fwd_sm90_generic` 的 entry 还是 `fwd_generic` 的 entry？

**参考答案**：取决于运行时该次调用是否请求 TMA 路径。查找时 `enable_tma` 是硬过滤条件（[_persistent_autotune.py:668-671](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L668-L671)）：运行时若 `enable_tma=True` 则只匹配 `fwd_sm90_generic`（`enable_tma=true`）的 entry，否则只匹配 `fwd_generic`（`enable_tma=false`）的 entry。两类 entry 互不串用。

**练习 3**：`device_config_path` 为什么不用 `compute_capability`（如 `sm_89`）做文件名，而用显卡名？

**参考答案**：同一 compute capability 下不同 SKU（如 L40 vs L20 都是 8.9）的 SM 数、显存、频率不同，最优 config 也不同；用显卡名做文件名实现「**设备级隔离**」，docs 明确「Do not reuse a JSON generated on one GPU class as a performance baseline for a different GPU class」（[autotune.md:384](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/user_guide/autotune.md#L384)）。`compute_capability` 仅作为 top-level 元数据与「无设备 JSON 时的架构回退」之用（架构回退属 u8-l3）。

---

## 5. 综合实践

**任务**：完整走一遍「离线生成 → 提交 → 运行时复用」的生产工作流，并把每一步对应到本讲的源码。

> 需 GPU。当前环境若无 GPU，请把所有命令视为「**待本地验证**」的脚本，重点放在「读源码确认命令行为」上。

1. **在目标显卡上生成全量配置**（前向 + 反向、两种 dtype、含变体）：

   ```bash
   CUDA_VISIBLE_DEVICES=0 python -m ffpa_attn.autotune \
     --mode fast --directions both --dtypes bf16,fp16 --full-tasks --overwrite
   ```

   对照 [main()](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L986-L1147) 回答：这条命令会进入 `_iter_forward_tasks` 还是 `_iter_backward_tasks`？（答：两个都进，因为 `--directions both`，见 [autotune.py:1006-1023](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L1006-L1023)。）

2. **确认输出文件**：检查 `src/ffpa_attn/triton/configs/{你的显卡名}.json` 是否生成，打开后核对 top-level 的 `schema_version`、`compute_capability`、`autotune_mode`、`full_tasks`、`tune_grid` 是否与你的命令一致。

3. **临时改写目录做对比**（不污染仓库）：用 `--output-dir /tmp/ffpa-cfg` 再生成一份只前向、bf16 的精简版；用 `FFPA_AUTOTUNE_MAX_CONFIGS=4` 截断以加速。

4. **运行时复用并验证命中**（此处用到 u8-l3 的查找，但验证手段属本讲 docs）：

   ```bash
   FFPA_LOGGER_LEVEL=DEBUG \
   FFPA_TUNED_CONFIG_DIR=/tmp/ffpa-cfg \
   python -m ffpa_attn.bench --fwd-backend triton --bwd-backend triton
   ```

   预期日志出现 `Persistent autotune cache hit` 或 `selected config` 行（[autotune.md:309-320](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/user_guide/autotune.md#L309-L320)），证明运行时确实读到了你刚生成的 JSON 而非内置默认 config。

**贯穿要点**：整个过程串起了本讲的 4 个最小模块——`_parse_args`（步骤 1 的参数）、`_iter_*_tasks`（网格）、`_iter_full_variant_tasks`（`--full-tasks` 变体）、`_entry_base`/`device_config_path`（步骤 2 的 JSON 与文件名）。

## 6. 本讲小结

- 持久化调优配置是「**离线生成、随包发布、运行时零调优复用**」的设备级 JSON，解决在线 autotune 启动慢、结果不持久的问题。
- `python -m ffpa_attn.autotune` 的全部能力在 `_parse_args`：`--mode`（必须与运行时一致）、`--directions`、`--dtypes`、`--B/--H`、`--full-tasks`、`--enable-*-tma/ws`（新旧两套，旧的是双向别名）、`--overwrite`、`--output-dir`、`--num-gpus/--ray-address`。
- 任务网格由 `DEFAULT_HEADDIMS=[320,512,640,768,1024]` 与 `DEFAULT_SEQLENS=[1,512,1024,2048,4096,8192,16384]` 张成；`1` 只作 decode 的 `Nq`，`16384` 需 ≥48 GiB 显存才纳入；prefill（`Nq≥512`）与 decode（`Nq=1`）两条分支分开生成。
- `--full-tasks` 追加 4 种**单特性**变体（attn-mask / dropout / gqa / mqa），不是笛卡尔积；GQA 的 KV 头数由 `_resolve_gqa_heads(heads)` 取 `heads//4` 向下找整除值（`H=32 → 8`）。
- 一条 entry 的核心字段是 `direction/kernel/causal/dtype/headdim/seqlen_q/seqlen_k/has_attn_bias/has_dropout/config`，外加 kernel 专属字段；`config` 是序列化后的最优 launch config。`_record_entry` 用元组 key 去重，`bwd_preproc` 故意不计入头数/mask/dropout。
- 输出文件名由 `sanitize_device_name(显卡名)+".json"` 决定（`NVIDIA L20 → NVIDIA_L20.json`），默认写到包内 `configs/`，双重不覆盖保护防止误冲。

## 7. 下一步学习建议

- 本讲只讲了「**怎么生成**」JSON，下一讲 **u8-l3（运行时配置查找与就近匹配回退）** 讲「**怎么用**」它：重点读 `_persistent_autotune.py` 的 `lookup_persistent_config` / `_lookup_persistent_config_cached`、`nearest_value`（head_dim 就近、并列取大）、`upper_or_max_value`（seqlen 取上界或最大），以及 `FFPA_TUNED_CONFIG_DIR` / `FFPA_SKIP_PERSISIT_TUNED_CONFIG` 等运行时环境变量。
- 若对多卡调优感兴趣，可跳读 **u8-l4（Ray 多 GPU 并行调优）**，看 `--num-gpus` 如何把本讲的 task 列表分发到多个 `TritonAutotuneWorker` actor 上并行执行。
- 想验证生成结果是否被正确命中，可结合 **u9-l2（持久化配置与自动调优模式测试）**，看 `tests/test_persistent_autotune_config.py` 如何注入临时 config 目录并断言查找结果。

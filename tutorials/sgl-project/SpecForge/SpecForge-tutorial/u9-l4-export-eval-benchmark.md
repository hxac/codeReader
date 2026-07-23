# 导出、评测与基准

## 1. 本讲目标

训练出草稿模型，故事只讲了一半。SpecForge 的产物是「运行时检查点（runtime checkpoint）」，它存的是**训练态**：草稿权重、优化器分片、global_step、RNG 状态……这并不是 SGLang 或 Hugging Face 能直接加载的服务目录。要让训练成果真正进入推理加速链路，必须经过最后三道工序：

- **导出（export）**：把运行时检查点物化成一份干净、自洽的服务模型目录。
- **评测（eval）**：在不启动服务的前提下，离线估计草稿的接受率，并据此挑出 best 检查点。
- **基准（benchmark）**：对真正跑起来的 SGLang 服务做端到端吞吐与投机解码遥测，量化加速比。

本讲学完后，你应当能够：

1. 说出 `specforge export --to hf` 与 `--to sglang` 的差异，解释为什么 sglang 导出要做权重键校验、哪些键是必需的。
2. 读懂 `Evaluator` 如何把一次 eval pass 聚合成 `eval/*` 指标、为什么默认用 `eval/simulated_acc_len` 选 best。
3. 读懂 `specforge benchmark` 如何度量一个在线 SGLang 服务的吞吐与接受长度，并能据此算出加速比。

本讲依赖 [u9-l1 检查点与恢复](u9-l1-checkpoint-resume.md)：导出和 best 选择的输入都来自 `CheckpointManager` 产出的 `{run_id}-step{N}` / `{run_id}-latest` 布局。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**运行时检查点 vs 服务目录。** 训练过程会把草稿权重、优化器状态、计数器等一起存进 `training_state.pt`（见 u9-l1）。服务侧的加载器（SGLang 的 EAGLE3 draft loader、HF 的 `from_pretrained`）只关心**纯模型权重**，对多余的键会直接报错或静默跳过。导出本质上是一道「翻译 + 裁剪」工序：把训练态剥离，只留服务态。

**接受率（acceptance）与接受长度。** 投机解码里，草稿每猜对一步就省一次大模型前向（见 [u1-l3 投机解码原理](u1-l3-speculative-decoding.md)）。能衡量「草稿好不好」的量有两个：单步接受率（第 *i* 步猜对的概率 \(a_i\)）和期望接受长度（一次草拟平均被接受几个 token）。SpecForge 的离线 eval 直接用训练时的 forward 复算每步准确率，再换算成期望接受长度——这就是 best 选择指标的来源。

**静态校验 vs 服务端实测。** 导出和离线 eval 都是「静态」的：不启动服务，只看权重和 forward 结果，快且确定。但「训练好的草稿在真实服务里到底快多少」是另一回事，受 KV cache、批调度、attention 后端影响，只能靠 `specforge benchmark` 对真实跑起来的服务端做实测。本讲三种工具恰好覆盖这条「离物化 → 离线估计 → 在线实测」的验证链。

下面用一个表格总览三者的定位：

| 工具 | 输入 | 是否启动服务 | 产出 | 价值 |
| --- | --- | --- | --- | --- |
| `export --to hf/sglang` | 运行时检查点 + draft config | 否 | 服务/HF 模型目录 | 物化产物，可被加载 |
| `Evaluator`（eval pass） | 验证集 + forward | 否 | `eval/*` 指标 + best 指针 | 离线选优 |
| `benchmark` | 在线 SGLang 服务 | 是（外部已启动） | 吞吐 / 接受长度 / verify 次数 | 实测加速 |

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `specforge/cli.py` | 定义 `export` 与 `benchmark` 两个子命令并分发 |
| `specforge/export/checkpoint_io.py` | 导出公共管线：从任意检查点路径解析训练态、实例化草稿模型 |
| `specforge/export/to_hf.py` | 导出为 HuggingFace 自洽目录（含 embedding 补全） |
| `specforge/export/to_sglang.py` | 导出为 SGLang 可加载目录（含权重键校验） |
| `specforge/eval/evaluator.py` | 把一次 eval pass 聚合成 `eval/*` 指标 |
| `specforge/training/checkpoint.py` | best 检查点选择（`best_metric` 默认值与判定） |
| `specforge/training/controller.py` | 训练循环里 eval → is_better → update_best 的接线 |
| `specforge/benchmarks/sglang.py` | 在线 SGLang 服务的吞吐与遥测基准 |

一句话总览调用链：

```
specforge export   → cli.py 分发 → export/to_{hf,sglang}.py → checkpoint_io.{resolve_training_state, materialize_draft}
specforge benchmark → cli.py 分发 → benchmarks/sglang.run → HTTP /generate + meta_info 解析
训练期 eval          → controller.evaluate_configured → eval/evaluator.Evaluator.run → checkpoint.is_better/update_best
```

## 4. 核心概念与源码讲解

### 4.1 导出 export：把运行时检查点物化为服务目录

#### 4.1.1 概念说明

`specforge export` 解决的问题是：训练态的检查点**不是**服务态的模型目录。两者的差别不止是「多了一些优化器键」那么简单，而是存在两类真实的坑：

1. **缺失必需权重。** 草稿检查点在训练侧被刻意剥离了冻结的 embedding（见 [u6-l2](u6-l2-train-strategy.md) 的 `checkpoint_state_filter`）。对 EAGLE 家族，草稿的 embedding 来自目标模型、训练时不更新、不存盘；对 DFlash 家族，草稿根本没有 embedding。但一份「自洽」的 HF 目录必须能在 `from_pretrained` 时补齐 embedding，否则服务会静默加载一个随机的 embedding，输出全错却不报错。

2. **权重命名漂移。** 训练侧的 state dict 可能带 `draft_model.` 前缀（FSDP 包裹残留），且不同草稿架构在「训练器里叫什么」和「服务加载器期望叫什么」之间可能不一致。SGLang 的加载是一个 **fail-silent 边界**：遇到不认识的键会**跳过或填零而不报错**，于是服务能起来，但草稿是坏的。SpecForge 用「显式权重映射表 + 必需键校验」把这个隐患从「上线后才发现」提前到「导出当场炸」。

`export` 提供两个目标格式：

- `--to sglang`：产出 SGLang EAGLE3 spec-decoder loader 能直接读的目录。**当前只支持 EAGLE3**（见下文 strategy 校验）。
- `--to hf`：产出自洽的 HF 目录，可被 `AutoDraftModel.from_pretrained` 重新加载（例如再 fine-tune）。覆盖 DFlash、Domino、DSpark、P-EAGLE 等家族。

两个格式都复用同一套「解析训练态 + 实例化草稿」的公共管线，差异只在裁剪与补全策略。

#### 4.1.2 核心流程

一次 `export` 的处理流程：

```
checkpoint_path（可能是 file/ dir/ output_dir/ file:// URI）
        │
        ▼
resolve_training_state()   ──► training_state dict（含 draft_state_dict、strategy）
        │
        ▼
materialize_draft()        ──► 用 draft_config 实例化草稿模型，load_state_dict(strict=False)
        │                     （unexpected 必报错；missing 仅容忍 embedding 与 t2d/d2t）
        ▼
┌──── 分流：--to hf / --to sglang ────┐
│                                      │
hf 分支：                               sglang 分支：
  · owns_embedding?                    · 校验 strategy == eagle3（否则报错）
  · 缺 embed_tokens.weight?            · 套用 WEIGHT_MAPS（EAGLE3 = identity）
    → 从 embedding_source 补            · _serving_state() 校验：
  · model.save_pretrained()              - 残留 draft_model. 前缀 → 报错
                                         - 缺 _REQUIRED_SERVING_KEYS → 报错
                                       · 剔除 embed* 键（服务复用目标 embedding）
                                       · save_pretrained()
```

两个分支共享的关键不变量：

- **unexpected 键必报错**：检查点里出现草稿架构没有的权重，说明架构与检查点不匹配。
- **embedding 是唯一被容忍的缺失**：因为训练侧刻意不存它。
- **重依赖（torch、transformers、modeling）懒导入**：只在真正导出时才加载，避免拖慢 `--plan` 等无 GPU 场景。

#### 4.1.3 源码精读

**(a) CLI 子命令定义与分发**

`export` 子命令只有 `hf` 和 `sglang` 两个选择，且把 `--embedding-source` 仅用于 hf 分支，在 sglang 分支会被显式拒绝：

[`specforge/cli.py:199-212`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L199-L212) 定义 `export` 的全部参数；[`specforge/cli.py:272-294`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L272-L294) 是分发逻辑——`args.to == "hf"` 走 `export_to_hf`，否则在确认没有误传 `--embedding-source` 后走 `export_to_sglang`。注意这里全部是**函数内懒导入**（`from specforge.export.to_hf import export_to_hf`），导出重依赖不会进入 train/benchmark 的 import 路径。

**(b) 公共管线 checkpoint_io**

[`specforge/export/checkpoint_io.py:23-71`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/export/checkpoint_io.py#L23-L71) 的 `resolve_training_state` 把「任意检查点形状的路径」统一成训练态字典，兼容四种输入：`training_state.pt` 文件、`{run_id}-step{N}` 目录、run `output_dir`（经 `{run_id}-latest` 指针解析，对应 u9-l1 的 `CheckpointManager` 布局）、以及带 `file://` 前缀的 URI。若没有 latest 指针（如无符号链接的文件系统），它回退到「最高 step 目录」，镜像 `CheckpointManager.latest_dir()` 的行为。

[`specforge/export/checkpoint_io.py:74-111`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/export/checkpoint_io.py#L74-L111) 的 `materialize_draft` 用 `AutoDraftModel.from_config`（见 [u4-l4](u4-l4-draft-model-registry.md)）以 bf16 实例化草稿，再 `load_state_dict(strict=False)`，随后做两道校验：

- `unexpected` 非空 → 直接 `ValueError`（架构对不上检查点）。
- `missing` 里只允许「含 embed 的键」与（传了 vocab mapping 时的）`t2d`/`d2t`；其它缺失必报错。

```python
# 只保留关键行
missing, unexpected = model.load_state_dict(state["draft_state_dict"], strict=False)
if unexpected:
    raise ValueError(...)                         # unexpected 必死
tolerated = {"t2d", "d2t"} if vocab_mapping_path else set()
non_embed_missing = [
    key for key in missing if "embed" not in key.lower() and key not in tolerated
]
if non_embed_missing:
    raise ValueError(...)                         # 非embedding缺失必死
```

**(c) to_sglang：权重键校验**

[`specforge/export/to_sglang.py:53-83`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/export/to_sglang.py#L53-L83) 的 `export_to_sglang` 有三道防线。第一道是 strategy 白名单——**只接受 EAGLE3**：

```python
# specforge/export/to_sglang.py:67-73
if state.get("strategy") != "eagle3":
    raise ValueError(
        "the specialized SGLang exporter currently supports EAGLE3 "
        f"checkpoints only, got strategy={state.get('strategy')!r}; use "
        "--to hf for DFlash-family and P-EAGLE draft model directories"
    )
```

第二道是权重映射。[`specforge/export/to_sglang.py:29-34`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/export/to_sglang.py#L29-L34) 定义了 `WEIGHT_MAPS` 与 `_REQUIRED_SERVING_KEYS`：

```python
WEIGHT_MAPS: Dict[str, Dict[str, str]] = {
    "LlamaForCausalLMEagle3": {},          # EAGLE3 是 identity 映射
}
_REQUIRED_SERVING_KEYS = ("fc.weight", "norm.weight", "lm_head.weight", "t2d", "d2t")
```

> 也就是说：对 `LlamaForCausalLMEagle3`，SpecForge 训练侧的草稿模块名（`midlayer.*` / `fc` / `norm` / `lm_head` + `t2d`/`d2t` buffer）**正好就是** SGLang EAGLE3 loader 要读的名字，所以映射是恒等。一个新架构（含未来的 MLA 草稿）必须先在 `WEIGHT_MAPS` 里加一条显式的、与 loader 版本匹配的映射，才能开启 sglang 导出。

第三道是 [`specforge/export/to_sglang.py:37-50`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/export/to_sglang.py#L37-L50) 的 `_serving_state`，它对映射后的 state dict 做两件事：**残留 `draft_model.` 前缀 → 报错**；**缺 `_REQUIRED_SERVING_KEYS` 中任一个 → 报错**。后者的报错信息明确点出「the sglang loader would silently produce a broken draft」——这正是把 fail-silent 隐患转成 fail-fast。

最后，[`specforge/export/to_sglang.py:81-82`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/export/to_sglang.py#L81-L82) 用 `if "embed" not in k.lower()` 剔除所有 embedding 键，与服务侧「复用目标模型 embedding」的契约一致。

**(d) to_hf：embedding 补全**

[`specforge/export/to_hf.py:64-113`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/export/to_hf.py#L64-L113) 的 `export_to_hf` 要解决的核心是「自洽」——HF 目录必须能在 `from_pretrained` 时不缺键。当检查点缺 `embed_tokens.weight` 时，它要求调用者提供 `embedding_source`（目标模型路径/目录），并优先尝试模型自带的 `load_embedding` 方法，否则用 `_load_embedding_tensor` 直接从目标权重里抠出单张 embedding：

```python
# specforge/export/to_hf.py:88-110（节选）
if owns_embedding and "embed_tokens.weight" not in state["draft_state_dict"]:
    if not embedding_source:
        raise ValueError("... pass embedding_source=<target model path> ...")
    load_embedding = getattr(model, "load_embedding", None)
    if load_embedding is not None:
        load_embedding(embedding_source, embedding_key=embedding_key)
...
full_state.update(state["draft_state_dict"])   # 训练过的键覆盖，最终落盘
model.save_pretrained(output_dir, state_dict=full_state)
```

[`specforge/export/to_hf.py:32-61`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/export/to_hf.py#L32-L61) 的 `_load_embedding_tensor` 只读单张 embedding 张量、**不**物化整个目标 `lm_head`：它先找 `*.index.json` 的 `weight_map` 定位 shard，没有 index 则要求目录里恰好一个权重文件；safetensors 用 `safe_open` 直读，`.bin` 走 `torch.load(weights_only=True)`。设计目的是「让 HF 导出便宜」——不必为了抠一张 embedding 就把整个目标模型加载进显存。

#### 4.1.4 代码实践

**实践目标：** 为一个 EAGLE3 检查点写出 sglang 与 hf 两条 export 命令，并验证权重键校验与 embedding 补全的行为。

**操作步骤：**

1. 假设你已经训出了一个 EAGLE3 run，检查点位于 `./outputs/qwen3-8b-eagle3/`，其中 `{run_id}-latest` 指针指向最新步。draft 配置在 `configs/qwen3-8b-eagle3.json`。

2. 执行 sglang 导出：

   ```bash
   specforge export --to sglang \
     --checkpoint ./outputs/qwen3-8b-eagle3/qwen3-8b-eagle3-latest \
     --draft-config configs/qwen3-8b-eagle3.json \
     --output-dir ./exports/qwen3-8b-eagle3-sglang
   ```

3. 执行 hf 导出（提供 `--embedding-source` 以补全冻结 embedding）：

   ```bash
   specforge export --to hf \
     --checkpoint ./outputs/qwen3-8b-eagle3/qwen3-8b-eagle3-latest \
     --draft-config configs/qwen3-8b-eagle3.json \
     --embedding-source Qwen/Qwen3-8B \
     --output-dir ./exports/qwen3-8b-eagle3-hf
   ```

4. 故意触发两条 fail-fast：先给 sglang 分支误传 `--embedding-source`，应当看到 `--embedding-source is only valid with --to hf`（见 [`cli.py:284-285`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L284-L285)）；再对一个非 EAGLE3 检查点（如 dflash）跑 `--to sglang`，应当看到「supports EAGLE3 checkpoints only」（见 [`to_sglang.py:68-73`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/export/to_sglang.py#L68-L73)）。

**需要观察的现象 / 预期结果：**

- 步骤 2、3 成功后，终端分别打印 `exported sglang draft to ...` / `exported HF draft to ...`，`--output-dir` 下出现 `config.json` + 权重文件。
- 步骤 2 的产物里**不含** `embed_tokens.weight`（被 [`to_sglang.py:81`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/export/to_sglang.py#L81) 剔除），但含 `fc.weight` / `norm.weight` / `lm_head.weight` / `t2d` / `d2t` 五个必需键。
- 步骤 4 的两条误用都被拦在导出阶段、非零退出码，而不是把坏目录写出去。

> 若本地暂无训练产物，可用参考检查点替换 `--checkpoint`（如 `zhuyksir/EAGLE3-Llama-3.1-8B-Instruct`），其余步骤不变；具体是否在本地跑通属「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `_REQUIRED_SERVING_KEYS` 里没有 `embed_tokens.weight`，而 hf 导出却要专门补它？

> **答案：** sglang 导出物**不自带** embedding——服务侧 EAGLE3 loader 复用目标模型的 embedding，故 SpecForge 在 [`to_sglang.py:81`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/export/to_sglang.py#L81) 把所有 `embed*` 键剔除；而 hf 导出物要满足 `from_pretrained` 的「自洽」要求（不缺键才能加载），所以必须从目标模型把真实 embedding 补进来（[`to_hf.py:88-110`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/export/to_hf.py#L88-L110)）。两者面向的加载器契约不同。

**练习 2：** 如果用 `--to sglang` 导出一个 `LlamaForCausalLMEagle3` 检查点，`WEIGHT_MAPS` 里对应的映射是 `{}`（恒等）。那这个映射表存在的意义是什么？

> **答案：** 它是「为未来扩展预留的显式契约」。当前 EAGLE3 训练侧命名恰好与服务侧一致所以是恒等；但任何新草稿架构（注释里点名「未来的 MLA draft」）都必须先在此登记一条与 SGLang loader 版本匹配的改名表，并在 `_REQUIRED_SERVING_KEYS` 上达成一致，才能安全开启 sglang 导出。没有映射表就会退成 fail-silent，这正是 SpecForge 要避免的。

**练习 3：** `materialize_draft` 用 `strict=False` 加载，却仍然能保证架构正确性，靠的是什么？

> **答案：** 靠两道显式校验：`unexpected` 非空必报错（检查点带了架构没有的键）；`missing` 里除了「含 embed」和（可选的）`t2d`/`d2t` 之外一律报错（[`checkpoint_io.py:93-106`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/export/checkpoint_io.py#L93-L106)）。`strict=False` 只是为了容忍「刻意不存的 embedding」，并非放松校验。

---

### 4.2 评测 evaluator：离线接受率估计与 best 选择

#### 4.2.1 概念说明

`Evaluator` 回答的问题是：「在不启动服务的前提下，怎么估计这个草稿有多好？」它的输入是验证集 forward 的结果，输出是一组 `eval/*` 指标，其中最重要的一项是 `eval/simulated_acc_len`——**期望接受长度**，也是 SpecForge 默认用来挑 best 检查点的指标。

关键概念是**逐位置准确率**。EAGLE3 这类 TTT（训练时测试）策略在 eval 时会连走若干步草拟（默认 `ttt_length` 步），每一步都有一个「猜对没」的统计。把第 *i* 步的准确率记作 \(a_i\)，整条草拟链的「期望被接受 token 数」就是：

\[
\mathbb{E}[\text{accepted}] = a_0 + a_0 a_1 + a_0 a_1 a_2 + \cdots = \sum_{k=1}^{n}\prod_{i=0}^{k-1} a_i
\]

直觉：第 0 步以概率 \(a_0\) 被接受；只有第 0 步被接受了，第 1 步才有机会以 \(a_1\) 被接受，于是两步都中的概率是 \(a_0 a_1\)；依此类推。这个值越大，说明草稿在真实投机解码里能省越多的大模型前向。**这就是 `simulated_acc_len`（模拟接受长度）的来历**——用静态 forward 的逐位置准确率，去模拟动态投机解码的期望接受长度。

对 DFlash/Domino 这类**标量策略**（没有逐位置 TTT，只算一个 batch 级准确率），`Evaluator` 退而用「按 `accuracy_denom` 加权的 batch 准确率均值」作为 `eval/avg_acc`，并把它直接当作 `simulated_acc_len`（见下文源码）。

`Evaluator` 还要处理分布式：eval 必须在所有 DP rank 上聚合后才正确，且每 rank 必须迭代**相同数量**的 eval batch（因为 forward 可能本身是 collective，如 FSDP）。

#### 4.2.2 核心流程

```
controller 每 eval_interval 步触发 evaluate_configured()
        │
        ▼ 对验证集每个 batch 调 strategy.forward_loss(batch, ctx)（见 u6-l2）
        ▼ 收集 StepOutput.metrics（含 acc_corrects/acc_denoms 或 accuracy）
Evaluator.run(forward_fn, batches)
        │
        ├─ 累加（local）：逐位置 [correct, denom, accept_rate*w, ploss*w]，float64 保精度
        ├─ collective（world_size>1）：
        │     (1) all_reduce(SUM) 标量累加
        │     (2) all_reduce(MAX) 逐位置长度（全局决策，空分片也参与）
        │     (3) all_reduce(SUM) 补零后的逐位置缓冲（iff 有任一 rank 有逐位置数据）
        │
        ▼
产出 metrics：
  TTT 策略   → eval/avg_loss, eval/avg_acc(=位置0), eval/per_position_acc,
               eval/simulated_acc_len, eval/acceptance_rate_i, eval/ploss_i
  标量策略   → eval/avg_loss, eval/avg_acc, eval/simulated_acc_len(=avg_acc)
        │
        ▼
CheckpointManager.is_better(metrics)   用 best_metric=eval/simulated_acc_len 判定
        │ is_better（rank0 判 + 广播，collective）
        ▼ True → update_best()：repoint {run_id}-best、写 best_meta.json
```

设计要点：

- **先求和后求比**：所有 correct/denom 计数在整个 eval 集合上求和之后才算比率，使结果与 batch 划分、DP 分片都无关（batch-size-invariant）。
- **float64 累加**：计数用 float64，保证超过 \(2^{24}\) 后仍精确。
- **零 batch 全局返回空**：如果全局一个 batch 都没跑，返回 `{}`，绝不伪造零指标——否则会污染 best 跟踪。

#### 4.2.3 源码精读

**(a) 主聚合逻辑**

[`specforge/eval/evaluator.py:35-159`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/eval/evaluator.py#L35-L159) 的 `run` 是核心。它维护两个累加器：逐位置矩阵 `pp`（4 行：correct / denom / acceptance_rate×w / ploss×w）与标量向量 `sums`（7 元：loss×w、w、scalar_acc×denom、scalar_denom、n_batches、ar_w、pl_w）。对每个 batch，它从 `out.metrics` 取数：

- 若策略产出 `acc_corrects` + `acc_denoms`（TTT 路径），累加进 `pp`，并按 token 权重累加 `acceptance_rates` 与 `plosses`。
- 否则若产出 `accuracy`（标量路径），按 `accuracy_denom`（缺失则按 token 数）加权累加进 `sums`。

```python
# specforge/eval/evaluator.py:129-159（零 batch 与最终指标）
if sums is None or sums[4].item() == 0.0:
    return {}                                   # 全局零 batch：不造零指标
loss_x_w, loss_w, acc_sum, acc_w, _n, ar_w, pl_w = sums.tolist()
avg_loss = loss_x_w / max(loss_w, 1.0)
if pp is not None:                              # TTT 路径
    per_position_acc = (pp[0] / pp[1].clamp_min(1.0)).tolist()
    metrics = {
        "eval/avg_loss": avg_loss,
        "eval/avg_acc": float(per_position_acc[0]),     # 位置0准确率
        "eval/per_position_acc": per_position_acc,
        "eval/simulated_acc_len": self._simulated_acc_len(per_position_acc),
    }
    ...
avg_acc = acc_sum / acc_w if acc_w else 0.0      # 标量路径
return {"eval/avg_loss": avg_loss, "eval/avg_acc": avg_acc,
        "eval/simulated_acc_len": avg_acc}        # 标量策略：simulated_acc_len = avg_acc
```

注意 TTT 路径下 `eval/avg_acc` 取的是**位置 0** 的准确率（第一步草拟的命中率），而 `eval/simulated_acc_len` 才是整条链的期望接受长度——两者含义不同。

**(b) 期望接受长度公式**

[`specforge/eval/evaluator.py:183-191`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/eval/evaluator.py#L183-L191) 实现了上面的乘积和：

```python
@staticmethod
def _simulated_acc_len(per_position_acc: List[float]) -> float:
    """E[accepted tokens] = a0 + a0*a1 + ... over the eval-set-wide
    per-position accuracy (length = ttt_length)."""
    cumulative, total = 1.0, 0.0
    for acc in per_position_acc:
        cumulative *= acc
        total += cumulative
    return total
```

即 \(\text{total} = \sum_{k=1}^{n}\prod_{i=0}^{k-1} a_i\)。该值的长度等于 `ttt_length`（训练时草拟步数），所以它衡量的是「在训练所用的草拟深度内」的期望接受长度。

**(c) 分布式 collective 调度**

[`specforge/eval/evaluator.py:107-127`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/eval/evaluator.py#L107-L127) 是固定三段式集体通信：(1) `all_reduce(SUM)` 标量 `sums`；(2) `all_reduce(MAX)` 各 rank 的逐位置长度（取最大值作为全局长度，**空分片也要参与**，保证每 rank 调度一致）；(3) 当全局长度 > 0 时，把各 rank 的 `pp` 补零到统一长度再 `all_reduce(SUM)`。模块 docstring（[`evaluator.py:9-14`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/eval/evaluator.py#L9-L14)）点明动机：collective 调度是全局决定的，空或纯标量分片也要发同样的 reduction，且 FSDP 下每 rank 必须迭代等量 batch。

通信设备由 [`evaluator.py:165-181`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/eval/evaluator.py#L165-L181) 的 `_comm_device` 按后端选：nccl→当前 CUDA 卡、hccl→`SPECFORGE_DEVICE=npu` 绑定的 NPU、否则 CPU（呼应 [u8-l1](u8-l1-distributed-init.md) 的后端选择）。

**(d) best 选择**

默认指标在 `CheckpointManager` 构造里硬编码：

```python
# specforge/training/checkpoint.py:36-49（节选）
def __init__(self, output_dir, run_id, *, max_checkpoints=0,
             best_metric="eval/simulated_acc_len", best_min_delta=0.0):
    ...
    self.best_metric = best_metric
    self.best_min_delta = best_min_delta
```

[`specforge/training/checkpoint.py:147-163`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L147-L163) 的 `score`/`is_better`：`score` 从 eval 指标里取 `best_metric` 的值；`is_better` 是 **collective**——rank0 判定「新分数 > best_score + best_min_delta」，再 `_bcast_flag` 广播给所有 rank，所以每 rank 的判决一致。空指标直接判 False。

[`specforge/training/checkpoint.py:165-182`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L165-L182) 的 `update_best`：更新 `best_score`/`best_step`，rank0 把 `{run_id}-best` 重指向该步目录，并把 `{run_id}.best_meta.json`（含 run_id/step/score/metric/全量 metrics）原子落盘。这个 meta 文件还用于 resume 时恢复 best（[`checkpoint.py:184-204`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L184-L204) 校验 run_id 与 metric 名一致，防止换指标后误用旧 best）。

**(e) 训练循环里的接线**

[`specforge/training/controller.py:600-622`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L600-L622) 把 eval 与 best 串起来：每 `eval_interval` 步跑 `evaluate_configured()` 得 `eval_metrics`；`is_best = eval_metrics 且 is_better(eval_metrics)`；只要 `interval_hit 或 is_best` 就存检查点，`is_best` 时再 `update_best`。注释强调 `is_better` 是 collective、guard 在各 rank 一致（因为 eval 指标是 DP 归约过的）。

#### 4.2.4 代码实践

**实践目标：** 通过阅读源码与一次受控实验，确认「默认 best 指标 = `eval/simulated_acc_len`」并理解其计算。

**操作步骤：**

1. 在 `specforge/eval/evaluator.py:183-191` 的 `_simulated_acc_len` 旁，手算一个例子：若逐位置准确率为 `[0.9, 0.8, 0.7]`，按公式
   \[
   \text{total} = 0.9 + 0.9\times0.8 + 0.9\times0.8\times0.7 = 0.9 + 0.72 + 0.504 = 2.124
   \]
   即期望接受约 2.12 个 token。

2. 阅读测试 [`specforge/training/checkpoint.py`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py) 与 controller 的 eval 分支，回答：如果一个 step 的 eval 指标里**没有** `eval/simulated_acc_len` 键，`is_better` 会返回什么？

3. （可选，需训练环境）跑一个极短 eval_interval 的 overfit run，在 `output_dir` 下观察 `{run_id}.best_meta.json`，确认其 `metric` 字段为 `eval/simulated_acc_len`，且 `score` 随训练单调上升。

**需要观察的现象 / 预期结果：**

- 步骤 1 手算与源码 `cumulative *= acc; total += cumulative` 的迭代结果一致。
- 步骤 2：`score` 返回 `None`（[`checkpoint.py:149`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L149)），`is_better` 判 `False`，该步不会更新 best——这与「零 batch 全局返回 `{}` 不污染 best」是同一套防护。
- 步骤 3 的 best_meta 文件随每次刷新 best 而更新（「待本地验证」是否在本地跑通）。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `Evaluator` 要「先在全集合求和 correct/denom，再算比率」，而不是每个 batch 先算准确率再平均？

> **答案：** 因为各 batch（以及各 DP 分片）的 token 数不同。先算比率再平均会被样本量小的 batch 不当地加权。先求和再求比相当于按 token 数加权，使结果与 batch 划分、DP 分片都无关（batch-size-invariant）。这正是模块 docstring 第一句强调的设计。

**练习 2：** TTT 策略下 `eval/avg_acc` 与 `eval/simulated_acc_len` 有何区别？为什么 best 指标选后者？

> **答案：** `eval/avg_acc` 是**位置 0**（第一步草拟）的准确率（[`evaluator.py:142`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/eval/evaluator.py#L142)），只反映单步命中率；`eval/simulated_acc_len` 是整条草拟链的**期望接受长度**（乘积和公式），直接对应「真实投机解码能省几次大模型前向」。后者与加速比的关系更直接，所以选它做 best。

**练习 3：** collective 调度里为什么要先 `all_reduce(MAX)` 逐位置长度，再补零 `all_reduce(SUM)`？

> **答案：** 不同 rank 的逐位置向量长度可能不同（取决于各自 batch 的草拟步数）。先取全局最大长度，再把短向量补零到该长度，才能保证「逐位置 i」在所有 rank 上指向同一位置，SUM 才有意义。这一步是「全局决策」，所以即便某个 rank 没有逐位置数据（空分片）也必须参与 MAX，这正是 docstring 说的「空分片也要发同样的 reduction」。

---

### 4.3 基准 benchmark：度量服务端吞吐与投机解码加速

#### 4.3.1 概念说明

`specforge benchmark` 回答最后一个问题：「这个草稿在真实 SGLang 服务里到底快多少？」它是**纯客户端**工具——不启动也不改配置服务，只对一个已经跑起来的 SGLang HTTP 服务发请求、量指标。

关键概念有三个：

1. **吞吐（throughput）**：单位时间产出的 token 数（tok/s），是加速比的核心度量。
2. **平均接受长度（average acceptance length）**：SGLang 在投机解码开启时，会通过 `meta_info` 返回每次请求的 `spec_accept_length`——平均每次草拟被接受几个 token。这正好是 4.2 里 `simulated_acc_len` 的**在线实测版**。
3. **投机验证次数（spec_verify_count）**：`spec_verify_ct`，即目标模型被触发去验证的总次数，反映投机解码的额外开销。

设计上有两条原则。其一，**算法无关**：runner 不关心草稿是 EAGLE3 还是 DFlash，只消费 SGLang「可选返回」的投机解码元数据——若服务是 target-only（没开投机解码），这些字段就是 None，工具退化为纯吞吐基准。其二，**可比性**：要量加速比，必须用**相同**的目标版本、tokenizer、chat template、prompts、采样参数、输出长度、硬件、TP、并发，分别基准 target-only 与 speculative 两个服务，再算吞吐比（见 `docs/benchmarks/benchmark.md`）。

> 与 `benchmarks/bench_eagle3.py` 的区别：那个脚本会**自己拉起/切换** SGLang 配置、跑多配置矩阵、还能评数据集准确率；`specforge benchmark` 更轻——只用一个已存在的服务、量吞吐与遥测。两者不要混比绝对吞吐。

#### 4.3.2 核心流程

```
specforge benchmark --model ... --dataset gsm8k --base-url ... --num-prompts N --concurrency C
        │
        ▼
_load_prompts(dataset, max_samples)   ──► 从 HF datasets 取提示，按 format lambda 格式化
        │                                   （multi_turn 如 mt-bench 取多轮）
        ▼
_apply_chat_template(tokenizer, ...)   ──► 用目标 tokenizer 渲染成服务期望的 prompt 串
        │                                   （enable_thinking 不支持时优雅降级）
        ▼
warmup：用 concurrency 个请求预热，排除 JIT/编译开销（不计入测量）
        │   并 /flush_cache 清服务缓存
        ▼
测量：ThreadPoolExecutor(concurrency) 并发发 --num-prompts 个请求
        │   收集每请求 meta_info：
        │     completion_tokens  → 累加 total_tokens
        │     spec_verify_ct     → 累加 verify_count
        │     spec_accept_length → 收集 acceptance_lengths
        ▼
BenchmarkResult：throughput = total_tokens / elapsed
                 average_acceptance_length = mean(acceptance_lengths)（或 None）
                 spec_verify_count（或 None）
        │
        ▼
_print_result 打印 + 可选 --output-json 落盘
```

要点：

- **预热与 flush**：先发 `concurrency` 个请求预热、`/flush_cache` 清缓存，再开始计时，避免冷启动与缓存命中污染吞吐。
- **确定性**：`random.seed(42)` 与 `random.Random(42).shuffle`（[`sglang.py:89`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/benchmarks/sglang.py#L89)）保证采样与打乱可复现。
- **并发由 ThreadPoolExecutor 控制**：每个请求一次 HTTP POST 到 `/generate`。

#### 4.3.3 源码精读

**(a) 数据集与结果结构**

[`specforge/benchmarks/sglang.py:20-56`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/benchmarks/sglang.py#L20-L56) 的 `DATASETS` 注册了五个数据集：`gsm8k`、`math500`、`humaneval`、`mbpp`、`mt-bench`（仅 mt-bench 标了 `multi_turn=True`），每条用 `format` lambda 把数据集行拼成提示串（如 gsm8k 会追加「Please reason step by step ... \boxed{}」）。

[`specforge/benchmarks/sglang.py:59-68`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/benchmarks/sglang.py#L59-L68) 的 `BenchmarkResult` 是不可变 dataclass，除吞吐等基础字段外，`average_acceptance_length` 与 `spec_verify_count` 都是 `Optional`——target-only 服务返回 None。

**(b) 请求与遥测提取**

[`specforge/benchmarks/sglang.py:107-125`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/benchmarks/sglang.py#L107-L125) 的 `_send_sglang` 发一次 POST 到 `{base_url}/generate`，sampling_params 透传 temperature/top_p/top_k/max_new_tokens；返回可能是列表，取首项。核心遥测在 [`sglang.py:174-183`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/benchmarks/sglang.py#L174-L183)：

```python
# specforge/benchmarks/sglang.py:174-183（节选）
for future in as_completed(futures):
    output = future.result()
    metadata = output.get("meta_info", {}) or {}
    total_tokens += int(metadata.get("completion_tokens", 0))
    verify_count += int(metadata.get("spec_verify_ct", 0))
    if metadata.get("spec_accept_length") is not None:
        acceptance_lengths.append(float(metadata["spec_accept_length"]))
```

三个字段都来自 SGLang 的 `meta_info`，且都是「有则用、无则忽略」——这就是「算法无关、消费可选元数据」的体现。

**(c) 预热与计时**

[`specforge/benchmarks/sglang.py:142-166`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/benchmarks/sglang.py#L142-L166) 先用 `warmup_count = concurrency` 个请求预热（`executor.map` 同步跑完），并在之前调 `/flush_cache` 清服务缓存（失败仅告警不中断）。之后 `prompts = prompts[warmup_count:]` 把预热请求踢出测量集合，`time.perf_counter()` 才开始计时。

**(d) 结果汇总与打印**

[`specforge/benchmarks/sglang.py:185-196`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/benchmarks/sglang.py#L185-L196) 汇总：`throughput = total_tokens / max(elapsed, 1e-12)`；`average_acceptance_length` 在有样本时取 `statistics.fmean`，否则 None；`spec_verify_count` 为 0 时也记 None。[`sglang.py:199-206`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/benchmarks/sglang.py#L199-L206) 的 `_print_result` 只在字段非 None 时打印接受长度与验证次数。[`sglang.py:209-217`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/benchmarks/sglang.py#L209-L217) 的 `run` 汇总统筹：`random.seed(42)`、跑 `_run_sglang`、打印、可选 `--output-json` 落盘（`asdict` + `sort_keys=True`）。

#### 4.3.4 代码实践

**实践目标：** 用 `specforge benchmark` 量一个 SGLang 服务的吞吐与投机解码遥测，并据此算加速比。

**操作步骤：**

1. 启动一个目标模型 + 你导出草稿的 SGLang 服务（speculative 配置），监听 `http://127.0.0.1:30000`。启动命令与 SGLang 版本相关，属服务端配置，本讲不展开。

2. 对该 speculative 服务跑基准：

   ```bash
   specforge benchmark \
     --model Qwen/Qwen3-8B \
     --dataset gsm8k \
     --base-url http://127.0.0.1:30000 \
     --num-prompts 256 \
     --concurrency 16 \
     --output-json ./bench-spec.json
   ```

3. 用**相同** model/tokenizer/数据集/采样参数/输出长度/并发，对一个 **target-only**（不开投机解码）服务再跑一次，输出到 `./bench-base.json`。

4. 比较两次的 `throughput_tokens_per_second`，加速比 = `spec_throughput / base_throughput`。

**需要观察的现象 / 预期结果：**

- speculative 服务的输出里应当出现 `Average acceptance length`（>1）与 `Speculative verify count`；target-only 服务这两行缺失（字段为 None）。
- `Output throughput` 的 speculative 值应高于 target-only 值，比值即加速比。
- 两次 run 的 `--num-prompts`、`--concurrency`、`--max-new-tokens`、`--dataset` 必须一致，否则不可比。

> 是否能跑通取决于本地是否已启动匹配的 SGLang 服务与硬件，属「待本地验证」。即便不跑，也可阅读 `sglang.py` 确认：runner 从不启动服务、只发 `/generate`，遥测三字段全部来自 `meta_info` 的可选读取。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `spec_verify_count` 与 `average_acceptance_length` 在结果里是 `Optional`？什么情况下会是 None？

> **答案：** 因为它们来自 SGLang **可选返回**的 `meta_info` 字段（`spec_verify_ct` / `spec_accept_length`）。当服务是 target-only（未开投机解码）或 SGLang 版本不返回这些字段时，`metadata.get(...)` 得 None，于是统计为空——`verify_count` 为 0 时记 None（[`sglang.py:195`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/benchmarks/sglang.py#L195)），`acceptance_lengths` 为空时均值取 None（[`sglang.py:192-194`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/benchmarks/sglang.py#L192-L194)）。这使同一工具既能基准投机解码、也能基准纯目标模型。

**练习 2：** 为什么基准前要先发 `concurrency` 个 warmup 请求并 `/flush_cache`，且把它们排除出计时？

> **答案：** 避免冷启动与缓存命中污染吞吐测量。首批请求可能触发 SGLang 的 CUDA graph 编译、kernel JIT 等一次性开销；KV cache 命中会让某些请求异常快。warmup 让服务进入稳态、`/flush_cache` 清掉缓存，之后再开始 `perf_counter` 计时，量到的才是稳态吞吐。

**练习 3：** `simulated_acc_len`（4.2）与 `average_acceptance_length`（4.3）衡量的是同一件事吗？

> **答案：** 是同一物理量的两种估计。`simulated_acc_len` 是**离线、静态**的——用训练 forward 的逐位置准确率按乘积和公式模拟出来的期望接受长度，用于训练期选 best；`average_acceptance_length` 是**在线、实测**的——SGLang 服务真正跑投机解码时 `meta_info` 返回的真实接受长度。前者快、免费、可作为代理指标；后者准、但需要起服务。理想情况下二者趋势一致。

## 5. 综合实践

把本讲三块串起来，走一遍「训完 → 导出 → 离线选优 → 在线实测」的完整验证链。假设你刚训完一个 EAGLE3 run（run_id = `my-eagle3`，output 在 `./outputs/my-eagle3`）。

1. **确认 best。** 查看 `./outputs/my-eagle3/my-eagle3.best_meta.json`，记下 `step` 与 `score`（指标 `eval/simulated_acc_len`）。这个 best 指针是训练期由 `Evaluator` + `CheckpointManager.is_better/update_best` 自动维护的（4.2）。

2. **导出 sglang 目录。** 用 best 或 latest 检查点导出（4.1）：

   ```bash
   specforge export --to sglang \
     --checkpoint ./outputs/my-eagle3/my-eagle3-latest \
     --draft-config configs/my-eagle3.json \
     --output-dir ./exports/my-eagle3-sglang
   ```

   导出器会校验 `fc.weight`/`norm.weight`/`lm_head.weight`/`t2d`/`d2t` 五个必需键，并剔除 embedding。

3. **（可选）导出 hf 目录。** 同一检查点导出一份自洽 HF 目录用于二次开发，需补 embedding：

   ```bash
   specforge export --to hf \
     --checkpoint ./outputs/my-eagle3/my-eagle3-latest \
     --draft-config configs/my-eagle3.json \
     --embedding-source Qwen/Qwen3-8B \
     --output-dir ./exports/my-eagle3-hf
   ```

4. **起 speculative 服务并用 benchmark 量加速。** 用步骤 2 的目录作为 SGLang 的 draft model 起服务，然后（4.3）：

   ```bash
   specforge benchmark --model Qwen/Qwen3-8B --dataset gsm8k \
     --base-url http://127.0.0.1:30000 --num-prompts 256 --concurrency 16 \
     --output-json ./bench-spec.json
   ```

   再对等量的 target-only 服务跑一次得 `./bench-base.json`，算加速比。

5. **交叉验证。** 比较步骤 1 的 `simulated_acc_len`（离线估计）与步骤 4 的 `average_acceptance_length`（在线实测）——两者应当量级接近、趋势一致。若离线高但在线低，通常说明服务端配置（draft tree、top-k、attention 后端）未对齐，而非训练问题。

> 步骤 2–4 是否能本地跑通取决于是否已具备训练产物、目标模型与 SGLang 服务环境，属「待本地验证」。即便不跑，本实践的源码阅读价值在于：看清「best 指针 → export 校验 → benchmark 遥测」三者如何共享同一套「接受长度」语义。

## 6. 本讲小结

- **export 是翻译+裁剪**：把训练态检查点物化成服务态目录。`--to sglang` 只支持 EAGLE3、用 `WEIGHT_MAPS` + `_REQUIRED_SERVING_KEYS` 把 SGLang 的 fail-silent 加载转成导出期 fail-fast；`--to hf` 面向全家族、靠 `embedding_source` 补全被刻意剥离的冻结 embedding 以保证自洽。
- **公共管线在 `checkpoint_io`**：`resolve_training_state` 兼容 file/dir/output_dir/`file://` 四类输入，`materialize_draft` 用 `strict=False` 但靠「unexpected 必报、missing 仅容忍 embedding」守住架构正确性。
- **Evaluator 先求和后求比**：用 float64 在全 eval 集合（跨 batch、跨 DP rank）累加 correct/denom 后才算比率，batch-size-invariant；collective 是固定三段式（SUM 标量、MAX 长度、SUM 补零逐位置），空分片也参与。
- **默认 best 指标是 `eval/simulated_acc_len`**：即期望接受长度 \(\sum_{k}\prod_{i<k} a_i\)，由逐位置准确率模拟而来，直接关联投机解码加速；零 batch 全局返回 `{}` 以免污染 best。
- **benchmark 是纯客户端实测**：不启动/不改服务，只对一个在线 SGLang 服务发请求，吞吐 = total_tokens / elapsed，并消费 `meta_info` 的可选遥测（`spec_accept_length` / `spec_verify_ct`），算法无关。
- **三种工具一条验证链**：export 物化、eval 离线估计、benchmark 在线实测，三者共享「接受长度」这一核心语义。

## 7. 下一步学习建议

- 想深入「best 选择」背后的检查点布局与恢复机制，复习 [u9-l1 检查点与恢复](u9-l1-checkpoint-resume.md)，重点看 `{run_id}-latest` / `{run_id}-best` 指针与 fork/rotation 语义。
- 想理解导出时草稿模型如何被实例化，回到 [u4-l4 草稿模型注册表](u4-l4-draft-model-registry.md)，看 `AutoDraftModel.from_config` 如何凭 `config.architectures` 查 `DRAFT_REGISTRY`。
- 想加一个新草稿架构并让它能导出到 SGLang，读 [u10-l1 新增一个草稿架构](u10-l1-custom-draft-arch.md) 后，回来给 `to_sglang.py` 的 `WEIGHT_MAPS` 与 `_REQUIRED_SERVING_KEYS` 加一条显式契约——这是「新架构开启 sglang 导出」的必经一步。
- 想做更系统的服务端评测（多配置矩阵、数据集准确率），阅读仓库内 `benchmarks/bench_eagle3.py` 与 `docs/benchmarks/benchmark.md`，那是比 `specforge benchmark` 更重的端到端基准。

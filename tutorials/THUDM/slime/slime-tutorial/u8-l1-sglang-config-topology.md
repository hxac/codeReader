# SGLang 拓扑与 sglang-config

## 1. 本讲目标

本讲解决一个问题：**当默认「单模型 + 一组均匀引擎 + 单 router」的推理部署不够用时，如何用一份 YAML 精确描述出异构的引擎拓扑？**

读完本讲你应该能够：

- 看懂 `--sglang-config` 这份 YAML 的三层结构（`sglang` → 模型 → server group），并理解每个字段的默认值与回退（fallback）链。
- 掌握 `ServerGroupConfig` 的两个核心字段：`worker_type`（决定引擎扮演什么角色）与 `num_gpus_per_engine`（决定单引擎张量并行 TP 大小）。
- 认识多模型部署时，每个模型各自拥有一个独立 router 的拓扑，以及 `prefill`/`decode` 这类异构组如何被 router 在内部调度。
- 独立写出一份合法的 sglang-config YAML，并能用纯 CPU 的单元测试方式验证它解析正确。

## 2. 前置知识

本讲建立在你已经学完 **u5-l3（SGLang 引擎封装与生命周期）** 的认知之上。请先回忆几个关键概念：

- **router（路由器）**：slime 用 SGLang 的 Model Gateway（`sgl-router`）作为前端负载路由器，rollout 函数不直接找某个引擎，而是把请求发到 router，由 router 转发给后端某个引擎。在 u5-l3 里我们看到默认情况下只有一个 router。
- **SGLangEngine**：一个 Ray actor，背后拉起一个真正的 SGLang HTTP 服务进程。引擎靠 `_register_to_router` 把自己的 `url` 和 `worker_type` 注册到 router。
- **on-policy / KV cache**：换权重前必须先 `flush_cache`，否则旧权重产出的 KV cache 会和新权重混用。

本讲要回答的新问题是：**当一个模型需要多种不同配置的引擎（比如 prefill 用小 TP、decode 用大 TP），或者一次要部署好几个模型（actor / reference / reward）时，slime 如何用配置而不是改代码来描述这种拓扑？**

这里再补两个术语，本讲会反复用到：

- **PD 分离（Prefill-Decode disaggregation）**：把「处理 prompt（prefill，计算密集）」和「逐 token 生成（decode，访存密集）」两个阶段拆给两组不同的引擎，各自按特性配置，以提升多轮 / 长尾场景吞吐。
- **TP（tensor parallel）/ DP / PP / EP**：SGLang 引擎自身的并行维度。本讲主要关心 TP，它由 `num_gpus_per_engine` 推导而来（细节见 4.2）。

## 3. 本讲源码地图

本讲涉及的文件很少，但分工明确：

| 文件 | 作用 |
|------|------|
| `slime/backends/sglang_utils/sglang_config.py` | **本讲主角**。三个 dataclass `SglangConfig` / `ModelConfig` / `ServerGroupConfig`，负责把 YAML 解析成结构化配置。 |
| `slime/ray/rollout.py` | 配置的「物化器」。`_resolve_sglang_config` 决定配置来源，`start_rollout_servers` 把配置变成真正的引擎与 router。 |
| `slime/backends/sglang_utils/sglang_engine.py` | `_compute_server_args` 把每个 group 的 `worker_type` 与 `overrides` 翻译成 SGLang `ServerArgs`（含 TP/PP 推导与 disaggregation 模式）。 |
| `slime/rollout/sglang_rollout.py` | `get_model_url`：自定义 rollout 函数里按模型名拿到对应 router 地址的工具。 |
| `slime/backends/sglang_utils/arguments.py` | 定义 `--sglang-config` 参数并做互斥校验。 |
| `docs/en/advanced/sglang-config.md` | 官方配置说明文档，含完整示例。 |
| `tests/utils/test_sglang_config.py` | 纯 CPU 单元测试，本讲代码实践的重要依据。 |

一句话导航：**YAML → `sglang_config.py` 解析成 dataclass → `rollout.py` 按 dataclass 建引擎和 router → `sglang_engine.py` 把每个引擎的细节参数算出来。**

## 4. 核心概念与源码讲解

### 4.1 SglangConfig / ModelConfig：配置的三层结构与 YAML 解析

#### 4.1.1 概念说明

`--sglang-config` 接收一个 YAML 文件。这个 YAML 是一个三层嵌套结构：

```
sglang:                  ← 顶层 key，固定写法
  - name: actor          ← 第 1 层：一个模型（ModelConfig）
    server_groups:
      - worker_type: prefill   ← 第 2 层：一组同构引擎（ServerGroupConfig）
        num_gpus: 4
      - worker_type: decode
        num_gpus: 12
```

- **第 1 层 `sglang`**：一个列表，每个元素是一个**模型**。一个 YAML 里可以定义多个模型。
- **第 2 层模型**：`name` 是唯一标识（如 `actor` / `ref` / `reward`）。一个模型挂在一个 router 后面。
- **第 3 层 `server_groups`**：一个列表，每个元素是**一组同构引擎**。同一模型下可以有多组配置不同的引擎（比如 prefill 组和 decode 组）。

这三层正好对应三个 dataclass：`SglangConfig`（整个文件）持有一组 `ModelConfig`（模型），每个 `ModelConfig` 持有一组 `ServerGroupConfig`（引擎组）。

设计动机：slime 想用「一份声明式配置」替代「写 Python 代码拼拓扑」。这样做的好处是把拓扑信息从训练代码里剥离，运维换拓扑只改 YAML 不改代码，也方便复用 slime 当纯粹的推理集群启动器。

#### 4.1.2 核心流程

配置从 YAML 到内存对象，再经过「解析（resolve）」补全默认值，流程如下：

```text
读 YAML → from_yaml()
   ├── 断言顶层有 'sglang' key
   ├── 对每个模型条目：
   │     ├── 取 name / model_path / num_gpus_per_engine / update_weights
   │     ├── 取 server_groups（或老别名 engine_groups）
   │     └── 每组构造成 ServerGroupConfig(**g)
   └── 返回 SglangConfig(models=[...])

→ 每个模型 .resolve(args)   # 在 start_rollout_servers 里被调用
   ├── GPU/engine 回退：组 → 模型 → args.rollout_num_gpus_per_engine
   ├── model_path 回退：组.overrides → 模型.model_path → args.hf_checkpoint
   ├── 校验：同一模型所有组必须共享同一个 model_path
   └── update_weights 自动推断：model_path 等于 hf_checkpoint → True，否则 False（并告警）
```

两个关键回退链（来自官方文档的 Resolution Rules，源码在 `resolve()` 与 `from_yaml()` 中一一对应）：

1. **GPU per engine 回退**：组的 `num_gpus_per_engine` → 模型的 `num_gpus_per_engine` → `args.rollout_num_gpus_per_engine`。
2. **model_path 回退**：组的 `overrides.model_path` → 模型的 `model_path` → `args.hf_checkpoint`。

还有一个**全局硬约束**：所有模型所有组的 `num_gpus` 之和，必须等于命令行的 `--rollout-num-gpus`，否则断言失败。

#### 4.1.3 源码精读

先看 `SglangConfig.from_yaml`，它是 YAML → dataclass 的入口：

[slime/backends/sglang_utils/sglang_config.py:L157-L180](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_config.py#L157-L180) —— 读 YAML、断言必须有 `sglang` 顶层 key，兼容老写法 `engine_groups`，逐模型构造 `ModelConfig`。

注意两个细节：第 169 行 `m.get("server_groups") or m.get("engine_groups") or []` 说明 `engine_groups` 是向后兼容的别名；第 170 行 `ServerGroupConfig(**g)` 直接把字典展开成构造参数，所以 YAML 里组的字段名必须和 dataclass 字段名（`worker_type` / `num_gpus` / `num_gpus_per_engine` / `overrides`）完全一致。

接着看 `ModelConfig.resolve`，它负责补全默认值与校验：

[slime/backends/sglang_utils/sglang_config.py:L68-L100](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_config.py#L68-L100) —— 三段回退（GPU/engine、model_path 注入、update_weights 自动推断）。

重点看 `update_weights` 的自动推断逻辑（第 90–100 行）：当用户没在 YAML 里写 `update_weights` 时，slime 会比较「该模型实际用的 model_path」和「`args.hf_checkpoint`（即被训练的模型）」。两者相等 → 这个模型就是被训练的对象，`update_weights=True`（每轮训练后要同步权重给它）；不等 → 这是冻结模型（reference/reward 等），`update_weights=False`，并打一条 warning 提醒你显式声明。这条逻辑解释了为什么多模型配置里 ref/reward 必须显式写 `update_weights: false`。

最后看几个派生属性，它们在后续物化时被用来决定 router 行为：

[slime/backends/sglang_utils/sglang_config.py:L102-L112](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_config.py#L102-L112) —— `has_pd_disaggregation`（任一组是 prefill/decode）、`has_encoder_disaggregation`（任一组是 encoder）、`total_num_gpus`（各组 num_gpus 求和）。

还有一条「兼容老 flag」的捷径 `from_prefill_num_servers`：

[slime/backends/sglang_utils/sglang_config.py:L182-L199](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_config.py#L182-L199) —— 把老的 `--prefill-num-servers` 翻译成一个等价的「default 模型 + prefill 组 + decode 组」配置。这解释了为什么 `--sglang-config` 和 `--prefill-num-servers` 互斥：两者描述的是同一件事，新版更灵活。

#### 4.1.4 代码实践

这个实践可以**纯 CPU 运行**，不需要 GPU，直接复用项目自带的单元测试套路。

1. **实践目标**：用 `SglangConfig.from_yaml` 解析一份多模型 YAML，并调用 `resolve()` 验证默认值回退与 `update_weights` 自动推断。
2. **操作步骤**：
   - 写一个临时 YAML 文件 `my.yaml`：
     ```yaml
     sglang:
       - name: actor
         server_groups:
           - worker_type: regular
             num_gpus: 8
       - name: ref
         model_path: /path/to/ref
         server_groups:
           - worker_type: regular
             num_gpus: 4
     ```
   - 在仓库根目录用 Python（已 `pip install -e .`）跑：
     ```python
     from argparse import Namespace
     from slime.backends.sglang_utils.sglang_config import SglangConfig

     cfg = SglangConfig.from_yaml("my.yaml")
     args = Namespace(hf_checkpoint="/path/to/actor", rollout_num_gpus_per_engine=2)
     for m in cfg.models:
         m.resolve(args)
     print(cfg.total_num_gpus)                 # 期望 12
     print(cfg.models[0].update_weights)        # 期望 True（actor == hf_checkpoint）
     print(cfg.models[1].update_weights)        # 期望 False（ref != hf_checkpoint）
     ```
3. **需要观察的现象**：`ref` 模型解析时会打印一条 warning（提示 model_path 与 hf_checkpoint 不符，默认 update_weights=False）。
4. **预期结果**：`total_num_gpus == 12`；actor 的 `update_weights` 为 `True`，ref 为 `False`。
5. 这个实践完全对应单测 [tests/utils/test_sglang_config.py:L73-L93](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/utils/test_sglang_config.py#L73-L93)，可直接 `pytest tests/utils/test_sglang_config.py -v` 对照。

#### 4.1.5 小练习与答案

**练习 1**：如果 YAML 里某个模型只写了 `name`、没写 `server_groups`，解析结果是什么？合法吗？

> **答案**：合法。`from_yaml` 会得到 `server_groups=[]`，`total_num_gpus` 计为 0。源码与单测 [tests/utils/test_sglang_config.py:L95-L106](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/utils/test_sglang_config.py#L95-L106) 表明：这种「空组」模型会暴露一个 router 但不创建本地引擎，常用于纯外部引擎接入。

**练习 2**：为什么 `from_prefill_num_servers` 里要 `assert decode_gpus > 0`？

> **答案**：因为若 prefill 占满了所有 GPU，就没有 decode 引擎，PD 分离不成立。该断言在 [sglang_config.py:L188](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_config.py#L188)，确保至少留有 decode 资源。

---

### 4.2 ServerGroupConfig：worker_type 与 num_gpus_per_engine 的语义

#### 4.2.1 概念说明

`ServerGroupConfig` 是配置树的最底层，描述「一组同构引擎」。它只有四个字段，但其中两个——`worker_type` 与 `num_gpus_per_engine`——是整份 YAML 里语义最重、最容易混淆的。

| 字段 | 含义 | 是否必填 |
|------|------|---------|
| `worker_type` | 这组引擎扮演什么角色 | **必填** |
| `num_gpus` | 这组一共占几张 GPU | **必填** |
| `num_gpus_per_engine` | 单个引擎用几张 GPU（即 TP 大小，可覆盖模型级默认） | 可选 |
| `overrides` | 覆盖 SGLang `ServerArgs` 字段的字典，优先级最高 | 可选 |

`worker_type` 的合法取值在源码里写死为五种（注意官方文档表格只列了前四种，`encoder` 是后续为 EPD 分离新增的）：

- `regular`：标准引擎，prefill 和 decode 都做（默认形态）。
- `prefill`：PD 分离的 prefill 工人，只处理 prompt。
- `decode`：PD 分离的 decode 工人，只做 token 生成，必须与 `prefill` 配对。
- `placeholder`：占位组，**只预留 GPU 槽位、不创建引擎**。
- `encoder`：EPD（Encoder-Prefill-Decode）分离的编码器组，多模态场景用，最先启动。

`num_gpus_per_engine` 是第二个关键点。直觉上它表示「一个引擎跨几张卡」，但它的真实身份是 **TP 大小的来源**：因为在 SGLang 里张量并行把模型切成多卡，一个引擎占几张卡就等于 `tp_size`（当 PP=1 时）。这一点会在 4.2.3 用源码证明。

#### 4.2.2 核心流程

一个 group 从配置到「TP 大小」的推导链：

```text
group.num_gpus_per_engine  (YAML 显式写)
        ↓ 若为 None
ModelConfig.num_gpus_per_engine  (模型级默认)
        ↓ 若为 None
args.rollout_num_gpus_per_engine  (命令行 --rollout-num-gpus-per-engine)
        ↓
得到 _gpus_per_engine
        ↓
pp_size = overrides['pp_size'] 或 args.sglang_pp_size（默认 1）
tp_size = _gpus_per_engine // pp_size        ← 这就是真正的 TP
```

所以当 `pp_size=1`（绝大多数场景）时：

\[ \text{tp\_size} = \text{num\_gpus\_per\_engine} \]

引擎实例数则由组的总 GPU 数除以单引擎卡数得到：

\[ \text{num\_engines} = \left\lfloor \frac{\text{num\_gpus}}{\text{num\_gpus\_per\_engine\_on\_node}} \right\rfloor \]

其中 `num_gpus_per_engine_on_node = min(num_gpus_per_engine, num_gpus_per_node)`，处理了单引擎跨多机的情况。

`overrides` 的优先级是「最高」：它会在所有基础参数（命令行 `--sglang-*` 透传、模型级默认）都设好之后**最后**应用，因此能覆盖一切。

#### 4.2.3 源码精读

先看 `ServerGroupConfig` 本体与它的自我校验：

[slime/backends/sglang_utils/sglang_config.py:L11-L41](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_config.py#L11-L41) —— 四个字段与 `__post_init__` 校验。

第 37–41 行的 `__post_init__` 在构造时立即断言 `worker_type` 必须属于五种合法值、`num_gpus > 0`。这意味着写错 `worker_type` 会在解析阶段就报错，而不是等到启引擎时。

接着看 TP/PP 推导，这是本讲最容易记错的地方。它发生在引擎参数计算函数里：

[slime/backends/sglang_utils/sglang_engine.py:L536-L540](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L536-L540) —— `tp_size` 由 `num_gpus_per_engine // pp_size` 推导。

第 538–539 行：`pp_size` 先从 overrides 取（默认 `args.sglang_pp_size`，即 1），`tp_size` 再由 `_gpus_per_engine // pp_size` 得到。这正是「`num_gpus_per_engine` 即 TP」的源码依据。

再看 `worker_type` 如何决定引擎的 disaggregation 模式（这是 PD 分离的内核）：

[slime/backends/sglang_utils/sglang_engine.py:L573-L584](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L573-L584) —— `worker_type` 到 disaggregation 模式的映射。

这段非常关键，逐条解释：

- `prefill`：设 `disaggregation_mode="prefill"`、负载均衡用 `follow_bootstrap_room`，并强制要求 `disaggregation_bootstrap_port`（这是 prefill/decode 之间握手用的端口）。
- `decode`：设 `disaggregation_mode="decode"`、`prefill_round_robin_balance=True`。
- `encoder`：设 `encoder_only=True`。

最后看 overrides 为何「优先级最高」——因为它在所有基础参数填充后才执行：

[slime/backends/sglang_utils/sglang_engine.py:L602-L620](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L602-L620) —— overrides 最后应用，逐键覆盖 `kwargs`。

注意第 606–611 行：YAML 里用连字符风格（如 `mem-fraction-static`）会被规范成下划线风格并打 warning，建议直接用下划线写法（`mem_fraction_static`）。

#### 4.2.4 代码实践

1. **实践目标**：验证「`num_gpus_per_engine` 即 TP」以及 overrides 能覆盖 TP/PP。
2. **操作步骤**：直接运行项目自带的两个 CPU 单测，它们正是为这个结论写的：
   ```bash
   pytest tests/utils/test_sglang_config.py \
     -k "test_server_group_parallel_config_derives_tp_from_overridden_pp \
         or test_sglang_server_args_derive_tp_from_overridden_pp" -v
   ```
3. **需要观察的现象**：两个测试都通过。重点看 `test_sglang_server_args_derive_tp_from_overridden_pp`：它给 `num_gpus_per_engine=32`、overrides 设 `pp_size=2`，断言最终 `tp_size==16`、`pp_size==2`。
4. **预期结果**：\(32 / 2 = 16\)，即 `tp_size=16`。这印证了公式 `tp_size = num_gpus_per_engine // pp_size`。
5. 源码见 [tests/utils/test_sglang_config.py:L193-L222](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/utils/test_sglang_config.py#L193-L222)。

#### 4.2.5 小练习与答案

**练习 1**：YAML 里某组写 `num_gpus: 8, num_gpus_per_engine: 4`，模型级没设、命令行 `--rollout-num-gpus-per-engine=2`。最终该组几个引擎？TP 多大？

> **答案**：组级显式写了 4，覆盖命令行的 2，所以单引擎 4 卡 → `tp_size=4`（pp=1）。引擎数 \(8/4=2\) 个。

**练习 2**：`placeholder` 组和 `regular` 组在引擎创建上有什么区别？

> **答案**：`placeholder` 组**完全不创建引擎**，只占 GPU 槽位（用于 colocate 训练时给训练留卡）。源码见 [slime/ray/rollout.py:L164-L166](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L164-L166)：`debug_train_only` 或 `worker_type == "placeholder"` 时直接返回空。

---

### 4.3 异构组拓扑：从配置到「引擎 + 多 router」的物化

#### 4.3.1 概念说明

前两模块讲的是「数据结构」，本模块讲「这些数据结构如何变成真实的进程」。这是 slime 推理拓扑最核心的部分，有三个关键设计：

1. **每个模型挂一个独立 router**。多模型部署时，slime 给每个模型各起一个 router（`sgl-router` 进程），模型之间在路由层完全隔离。自定义 rollout 函数通过模型名找到对应 router。
2. **router 内部按 worker_type 调度异构组**。同一个模型下的 prefill 组和 decode 组都注册到同一个 router；router 因为被设了 `pd_disaggregation=True`，知道要把请求的 prefill 阶段送给 prefill 工人、decode 阶段送给 decode 工人。
3. **EPD（含 encoder）需要两阶段启动**。如果模型里有 `encoder` 组，必须先把 encoder 引擎起来、拿到它们的 URL，再把这些 URL 注入到 prefill/regular 组的 `encoder_urls` 参数里，因为语言模型引擎启动时就要知道去哪找编码器。

#### 4.3.2 核心流程

`start_rollout_servers` 是物化的总入口，外层循环遍历每个模型，内层处理每个组：

```text
start_rollout_servers(args, pg):
  config = _resolve_sglang_config(args)          # 选配置来源
  for model_idx, model_cfg in enumerate(config.models):
      model_cfg.resolve(args)
      has_pd  = model_cfg.has_pd_disaggregation
      has_epd = model_cfg.has_encoder_disaggregation
      # 关键：第 0 个模型复用默认 router；之后每个模型 force_new 起新 router
      router_ip, router_port = _start_router(args, has_pd, force_new=(model_idx>0))
      if has_epd:
          阶段1：先启动所有 encoder 组，ray.get 等就绪，收集 encoder_urls
          阶段2：启动 prefill/regular 组，把 encoder_urls + language_only 注入 overrides
      else:
          一次性启动所有组
      servers[model_cfg.name] = RolloutServer(server_groups, router_ip, router_port, ...)
  args.sglang_model_routers = {name: (ip, port) for ...}   # 暴露给自定义 rollout
```

几个要点：

- **多 router**：`force_new=(model_idx > 0)` 保证除第一个模型外，每个模型都新开一个 router 端口；第一个模型写回 `args.sglang_router_ip/port` 作向后兼容。
- **PD 路由**：`has_pd_disaggregation=True` 时，`_start_router` 会把 router 的 `pd_disaggregation` 标志置真，router 就会按 prefill/decode 两阶段调度。
- **引擎注册**：每个引擎启动后调 `_register_to_router`，把自己的 `url` 和 `worker_type`（以及 prefill 的 `bootstrap_port`）POST 给 router 的 `/workers`，router 据此区分 prefill/decode 工人。
- **模型寻址**：物化结束后，`args.sglang_model_routers` 是一个 `{模型名: (ip, port)}` 字典；自定义 rollout 用 `get_model_url(args, "ref")` 拿到 ref 模型的 router 地址。

#### 4.3.3 源码精读

先看配置来源选择 `_resolve_sglang_config`，它体现了「四选一」的优先级：

[slime/ray/rollout.py:L1234-L1258](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1234-L1258) —— 配置来源优先级：`--sglang-config` YAML → 零 GPU 空配置 → `--prefill-num-servers` → 默认单 regular 组。

注意第 1239–1241 行的全局校验：YAML 的 `total_num_gpus` 必须等于 `args.rollout_num_gpus`，否则断言失败——这就是本讲前面强调的硬约束。

接着看物化主循环，重点是多 router 与 force_new：

[slime/ray/rollout.py:L1120-L1131](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1120-L1131) —— 每个模型先 `resolve`，再按 `has_pd` 起 router，`force_new=(model_idx>0)` 让多模型各自有独立 router。

第 1124 行是「多 router」的核心：第一个模型用默认 router（写回 args），后续模型 `force_new=True` 各开新端口。第 1127–1129 行把第一个模型的 router 写回 `args.sglang_router_ip/port`，保证不读 sglang-config 的旧代码仍能工作。

看 `_make_group`，它把一个 group_cfg 变成 `ServerGroup` 运行时对象，并计算关键的 `needs_offload`、引擎数、offset：

[slime/ray/rollout.py:L1136-L1172](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1136-L1172) —— 计算引擎数、`needs_offload`、offset，构造 `ServerGroup`。

第 1140 行 `num_engines = group_cfg.num_gpus // num_gpus_per_engine_on_node` 正是 4.2.2 里引擎数公式的实现；第 1142–1143 行 `needs_offload` 判断该组的 GPU 是否和训练卡重叠（colocate 场景）。注意 `engine_offset` 和 `gpu_offset` 是跨组累加的，保证多组引擎的 rank 与 GPU 起始位置连续不重叠。

看 EPD 两阶段启动——这是含 encoder 模型的特殊路径：

[slime/ray/rollout.py:L1174-L1208](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1174-L1208) —— 阶段 1 同步启动 encoder 组并收集 URL；阶段 2 把 `encoder_urls` 与 `language_only` 注入 prefill/regular 组再启动。

这段说明：encoder 引擎必须先启动并暴露 URL，因为这些 URL 要作为参数喂给语言模型引擎。对应的单测在 [tests/utils/test_sglang_config.py:L312-L385](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/utils/test_sglang_config.py#L312-L385)，断言 encoder 在 regular 之前完成、且 regular 组的 overrides 含 `encoder_urls` 与 `language_only=True`。

看物化结束暴露的模型→router 映射，这是自定义 rollout 寻址的依据：

[slime/ray/rollout.py:L1228-L1229](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1228-L1229) —— `args.sglang_model_routers = {模型名: (ip, port)}`。

再看 router 端如何根据 PD 标志切换调度模式：

[slime/ray/rollout.py:L1050-L1051](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1050-L1051) —— `has_pd_disaggregation` 为真时，把 router 的 `pd_disaggregation` 置真。

最后看引擎如何把自己的角色告诉 router——这是 router 能区分 prefill/decode 的根本：

[slime/backends/sglang_utils/sglang_engine.py:L194-L216](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L194-L216) —— 引擎向 router 的 `/workers` 注册 `url` + `worker_type`；prefill 额外带 `bootstrap_port`。

第 200–203 行 payload 含 `worker_type`，router 据此把工人归类；第 204–211 行 prefill 工人必须带 `bootstrap_port`，这是它和 decode 工人握手传 KV cache 用的。encoder 组（第 195–196 行）跳过注册。

看自定义 rollout 如何按模型名寻址：

[slime/rollout/sglang_rollout.py:L64-L80](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L64-L80) —— `get_model_url` 从 `args.sglang_model_routers` 取地址，找不到则回退默认 router。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：追踪「一次 PD 分离请求」在源码里的路由依据，搞清 router 凭什么把请求分给 prefill 还是 decode。
2. **操作步骤**：
   - 阅读 [slime/backends/sglang_utils/sglang_engine.py:L573-L584](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L573-L584)：确认 prefill 引擎带 `disaggregation_mode="prefill"`、decode 带 `disaggregation_mode="decode"`。
   - 阅读 [slime/backends/sglang_utils/sglang_engine.py:L194-L216](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L194-L216)：确认两类引擎注册时都上报了 `worker_type`，prefill 还上报 `bootstrap_port`。
   - 阅读 [slime/ray/rollout.py:L1050-L1051](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1050-L1051)：确认 router 被设了 `pd_disaggregation=True`。
3. **需要观察的现象**：把三段串起来，你能解释 router 调度的依据：router 因为 `pd_disaggregation=True` 进入 PD 模式，又因为 prefill/decode 引擎各自上报了 `worker_type`，于是 router 把请求的 prefill 阶段路由给某个 prefill 工人（经 `follow_bootstrap_room` 配对），把 KV cache 经 `bootstrap_port` 传给配对的 decode 工人继续生成。
4. **预期结果**：用自己的话写出「router 如何在 prefill 组与 decode 组之间路由」的一段说明（参考综合实践的答案）。
5. 若无法本地起 SGLang 集群验证，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：多模型配置里，为什么每个模型要独立 router，而不是所有模型共用一个 router？

> **答案**：模型间在路由层隔离，便于各自独立负载均衡与容错；更重要的是 PD 分离的 `pd_disaggregation` 标志是 router 级别的，不同模型可能一个要 PD、一个不要，必须分开。源码 `force_new=(model_idx>0)` 体现了这一点。

**练习 2**：`get_model_url(args, "unknown")` 时 `sglang_model_routers` 里没有 `unknown`，会发生什么？

> **答案**：不会报错，回退到默认 router（`args.sglang_router_ip:port`）。见 [sglang_rollout.py:L76-L80](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L76-L80)，单测 [test_sglang_config.py:L407-L418](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/utils/test_sglang_config.py#L407-L418) 印证。

**练习 3**：`--sglang-config` 能和 `--prefill-num-servers` 同时用吗？

> **答案**：不能，二者互斥。校验在 [slime/backends/sglang_utils/arguments.py:L183-L185](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L183-L185)。原因如 4.1 所述：`--prefill-num-servers` 是 PD 分离的老接口，`--sglang-config` 是更灵活的新接口，二者描述同一件事。

## 5. 综合实践

**任务**：写一个 sglang-config YAML，让一个模型包含 TP=2 的 prefill 组与 TP=4 的 decode 组，并说明 router 如何在二者间路由。

**参考 YAML**（基于 [docs/en/advanced/sglang-config.md:L103-L114](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/advanced/sglang-config.md#L103-L114)）：

```yaml
# sglang_pd.yaml
sglang:
  - name: actor
    server_groups:
      - worker_type: prefill
        num_gpus: 4
        num_gpus_per_engine: 2    # 4 / 2 = 2 个 prefill 引擎，每个 TP=2
      - worker_type: decode
        num_gpus: 12
        num_gpus_per_engine: 4    # 12 / 4 = 3 个 decode 引擎，每个 TP=4
```

对应启动命令（`--rollout-num-gpus` 必须等于各组 num_gpus 之和 \(4+12=16\)）：

```bash
python train.py \
  --sglang-config sglang_pd.yaml \
  --rollout-num-gpus 16 \
  ...
```

**请读者完成并验证的子任务**：

1. **验算引擎数与 TP**：prefill 组 \(4/2=2\) 引擎、TP=2；decode 组 \(12/4=3\) 引擎、TP=4。依据公式 `tp_size = num_gpus_per_engine // pp_size`（pp=1）。
2. **纯 CPU 校验配置合法**：用 4.1.4 的方法 `SglangConfig.from_yaml("sglang_pd.yaml")` 解析，确认 `cfg.total_num_gpus == 16`、`cfg.has_pd_disaggregation is True`。
3. **说明 router 如何路由**（用文字作答，参考答案如下）。

**router 路由说明（参考答案）**：

- 该模型 `has_pd_disaggregation=True`，`start_rollout_servers` 会用 `_start_router(..., has_pd_disaggregation=True, ...)` 启动 router，router 进入 PD 模式（[rollout.py:L1050-L1051](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1050-L1051)）。
- 5 个引擎启动后各自向 router 的 `/workers` 注册，prefill 引擎带 `worker_type=prefill` 和 `bootstrap_port`，decode 引擎带 `worker_type=decode`（[sglang_engine.py:L194-L216](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L194-L216)）。
- 一个请求到来时：router 先把它交给某个 prefill 引擎处理 prompt（prefill 引擎用 `follow_bootstrap_room` 负载均衡），prefill 完成后通过 `bootstrap_port` 把 KV cache 传给一个配对的 decode 引擎，由 decode 引擎逐 token 生成并返回（prefill/decode 的 disaggregation 模式见 [sglang_engine.py:L573-L584](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L573-L584)）。
- 由于 prefill 用小 TP（吞吐优先）、decode 用大 TP（延迟优先），两组可以独立扩缩容——这正是 PD 分离的收益。

> 若本地无 16 卡环境，第 1、2 步可在纯 CPU 完成，第 3 步为源码阅读型说明；端到端运行效果「待本地验证」。

## 6. 本讲小结

- `--sglang-config` 是一个三层 YAML（`sglang` → 模型 → server group），对应三个 dataclass `SglangConfig` / `ModelConfig` / `ServerGroupConfig`，把推理拓扑从代码里声明式剥离。
- `ServerGroupConfig.worker_type` 决定引擎角色（regular/prefill/decode/placeholder/encoder），`num_gpus_per_engine` 决定 TP 大小（`tp_size = num_gpus_per_engine // pp_size`），`overrides` 优先级最高、最后应用。
- `resolve()` 实现两条回退链（GPU/engine、model_path）并自动推断 `update_weights`：与 `hf_checkpoint` 相等的模型收权重更新，其余冻结。
- 物化时**每个模型挂一个独立 router**（`force_new=(model_idx>0)`），PD 分离模型会把 router 设为 `pd_disaggregation=True`，由引擎上报的 `worker_type` 让 router 在 prefill 与 decode 组之间分阶段路由。
- 全局硬约束：所有组 `num_gpus` 之和必须等于 `--rollout-num-gpus`；`--sglang-config` 与 `--prefill-num-servers`、`--rollout-external-engine-addrs` 互斥。
- 含 `encoder` 组的 EPD 拓扑需两阶段启动：先把 encoder 起好收集 URL，再注入到 prefill/regular 组的 `encoder_urls`。

## 7. 下一步学习建议

- 本讲聚焦「拓扑如何描述与物化」，但没讲 PD 分离在**跨机 / 外部集群**的部署细节，以及 `consistent_hashing` 路由对多轮 agent 的前缀缓存收益——这正是下一讲 **u8-l2（PD 分离与外部推理引擎）** 的主题，建议紧接着学。
- 若你想了解 router 端更细的会话亲和（`session_id` → `X-SMG-Routing-Key`）机制，可先回顾 u7-l2（Agent 运行时适配器），那里讲了 `session_id` 如何驱动前缀缓存路由。
- 想验证自己写的配置，最省事的方式是跑 `pytest tests/utils/test_sglang_config.py -v`（纯 CPU），它覆盖了本讲提到的 `update_weights` 推断、零 GPU、EPD 两阶段、`get_model_url` 回退等全部关键路径。

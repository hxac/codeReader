# 权重加载 weight_loader

## 1. 本讲目标

本讲要回答一个具体问题：**磁盘上 HuggingFace 格式的权重文件，是怎么被「灌进」上一讲（u3-l3、u3-l4）构造好的 patched model 里的？**

学完后你应该能够：

- 说出从 `Engine.from_pretrained` 到「权重真正落在显存里」的完整调用链。
- 读懂 `ModelWeightLoader` 如何识别 safetensors / PyTorch、单文件 / 分片（sharded）四种组合。
- 理解「参数级加载契约」：模型侧的 `load_weights` 如何借助 `load_weight` 和 `param.weight_loader` 属性，把 HF 的分片权重塞进 `qkv_proj`、`gate_up_proj` 这类「打包参数」，并完成张量并行（TP）切分。
- 认识加载完成后的「收尾动作」：跳过 dummy 模块、`rename_weight` 改名、`update_weights` 二次整理、`torch.inference_mode` 推理模式。

> 前置衔接：本讲假设你已读过 u3-l3（Patch 重写机制：`build_patched_model`、`MODULE_MAP`）与 u3-l4（以 Llama 为例的重写实现：`qkv_proj`/`gate_up_proj` 打包、`load_weights`、`stacked_params_mapping`）。本讲就是那条链的「最后一公里」。

## 2. 前置知识

在进入源码前，先用三段话把背景讲清楚。

**第一，什么叫「灌权重」。** 上一讲的 patched model 是一个**结构已定、但参数为空（或随机）**的 `torch.nn.Module`。它的每一层（`qkv_proj`、`gate_up_proj`、`o_proj`、`down_proj`…）都已经在显存里开了好形状的「空格子」，只是格子里的数值还不对。权重加载的任务，就是把磁盘上 HF 仓库里 `*.safetensors` 文件中的张量，**按名字匹配**拷贝进这些空格子。本质上是一次大规模的 `param.data.copy_(loaded_tensor)`。

**第二，为什么不能直接 `model.load_state_dict(...)`。** 因为 LMDeploy 对模型做了「改造」（见 u3-l4）：

- HF 里 q、k、v 是三个独立权重 `q_proj.weight`、`k_proj.weight`、`v_proj.weight`；LMDeploy 把它们**融合**成一个 `qkv_proj` 以减少访存。
- HF 里 gate、up 是两个权重；LMDeploy 融合成 `gate_up_proj`。
- 张量并行（TP）下，每个 rank 只需要权重的一部分（列切或行切）。

于是 HF 权重名字 → LMDeploy 参数名字不是一一对应，需要一个「翻译 + 切片」过程。这正是本讲 `load_weights` + `load_weight` 要解决的核心矛盾。

**第三，四种权重文件形态。** HuggingFace 仓库里的权重文件有四种常见组合，由两个维度决定：

| 维度 | 取值 A | 取值 B |
|------|--------|--------|
| 格式 | `safetensors`（`.safetensors`，推荐、可零拷贝） | `pytorch`（`.bin`，`torch.save` 产物） |
| 是否分片 | 单文件（`model.safetensors`） | 分片（`model.safetensors.index.json` + 多个分片） |

`ModelWeightLoader` 第一件事就是把这四种形态识别出来并统一成「一个文件路径的迭代器」。

> 术语：**safetensors** 是 HuggingFace 推出的安全权重格式，文件头是 JSON 描述各张量的偏移与形状，正文是裸字节，可被 `mmap` 直接映射，加载快且能避免反序列化任意代码的风险。**shard（分片）** 是把一个大模型拆成多个小文件（如 `model-00001-of-00005.safetensors`），便于单文件不超过 GitHub/HF 的 5GB / 50GB 限制。

## 3. 本讲源码地图

本讲涉及的关键文件，按「从上到下」的调用顺序排列：

| 文件 | 作用 |
|------|------|
| [`lmdeploy/pytorch/engine/engine.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py) | PyTorch 引擎主类。`from_pretrained` 是用户入口；`__init__` 把建模型交给 `build_executor`。 |
| [`lmdeploy/pytorch/engine/model_agent/agent.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py) | 每个 GPU rank 上真正干活的角色。`_build_model` 先 `build_patched_model` 再调 `load_model_weights`。 |
| [`lmdeploy/pytorch/weight_loader/model_weight_loader.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py) | **本讲主角**。`ModelWeightLoader` 类识别格式/分片并迭代权重；`load_weight`/`default_weight_loader` 做参数级拷贝；模块级 `load_model_weights` 是顶层函数。 |
| [`lmdeploy/pytorch/models/llama.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py) | patched model 样本。其 `load_weights` 方法消费权重迭代器，是 loader 与模型对接的「接口」。 |
| [`lmdeploy/pytorch/nn/linear/default.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/default.py) | 优化线性层。它在 `setup_loaders` 里给参数挂上 `weight_loader` 属性，这是「参数级加载契约」的注册点。 |
| [`lmdeploy/pytorch/models/utils/model.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/utils/model.py) | 所有 patched model 的基类，定义 `rename_weight`/`update_weights`/`load_weights` 默认行为。 |

## 4. 核心概念与源码讲解

### 4.1 引擎层入口：from_pretrained 到权重加载的调用链

#### 4.1.1 概念说明

用户写 `pipeline('Qwen/Qwen2.5-7B-Instruct')` 时，最终会走到 `PytorchEngine` 的构造。引擎**不亲自**建模型、也不亲自读权重——它把这两件事委托给一个叫 **executor / model agent** 的执行单元。权重加载发生在 model agent 内部，紧接在「建空模型」之后。

这条链的关键节点是：

```
Engine.from_pretrained(model_path)          # 用户入口（类方法）
   └─> Engine.__init__(model_path, ...)     # 不碰权重，只搭框架
         └─> build_executor(...)            # 建执行器，把活儿外包
               └─> ModelAgent._build_model()
                     ├─ build_patched_model(...)   # u3-l3/u3-l4：建「空格子」模型
                     └─ load_model_weights(...)    # 本讲：往格子里灌权重 ★
```

> 注意职责分层：`from_pretrained` 只是「转发参数」的薄壳；真正的「建模型 + 灌权重」在 `_build_model`。这种「入口轻、executor 重」的分层，是为了让多卡（TP/DP）下每个 rank 能各自跑一份 model agent。

#### 4.1.2 核心流程

1. `Engine.from_pretrained` 接收模型路径与 `PytorchEngineConfig`。
2. 若启用了 `enable_mp_engine`（多进程引擎），走另一条 `build_mp_engine` 分支；否则调用 `cls(...)`，即 `__init__`。
3. `__init__` 里调用 `build_executor(...)` 构建 executor 并 `executor.init()`——这一步会在每个 rank 上拉起 model agent。
4. model agent 的 `_build_model` 先 `build_patched_model(...)` 造出 patched model（u3-l3），随后调用模块级 `load_model_weights(patched_model, model_path, device=device)`。

#### 4.1.3 源码精读

**入口类方法 `from_pretrained`**：它几乎是纯转发，关键只在 `enable_mp_engine` 分支与最后的 `cls(...)`：

[engine.py:219-258](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L219-L258) —— 用户层入口，把模型路径透传给构造器。

```python
@classmethod
def from_pretrained(cls, pretrained_model_name_or_path, engine_config=None,
                    speculative_config=None, trust_remote_code=False, **kwargs):
    if engine_config is not None and engine_config.enable_mp_engine:
        from .mp_engine import build_mp_engine
        return build_mp_engine(...)              # 多进程引擎走特殊分支
    return cls(model_path=pretrained_model_name_or_path,
               engine_config=engine_config, ...) # 普通：交给 __init__
```

**`__init__` 中把建模型外包给 executor**：注意 `__init__` 里**没有任何 `load` 字样**，它只调用 `build_executor` + `executor.init()`，模型与权重都在 executor 内部（即 model agent）完成：

[engine.py:152-165](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L152-L165) —— `__init__` 把 model_path 等交给 `build_executor`，自己不碰权重。

```python
self.executor = build_executor(model_path, cache_config=..., backend_config=...,
                               dist_config=..., adapters=adapters, ...)
self.executor.init()
```

**真正建模型并灌权重的地方——model agent 的 `_build_model`**：这是全篇最关键的一处，承接了 u3-l3 的 `build_patched_model` 与本讲的 `load_model_weights`：

[model_agent/agent.py:1101-1131](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py#L1101-L1131) —— 先建 patched model，再调 `load_model_weights` 灌权重。

```python
def _build_model(self):
    ...
    patched_model = build_patched_model(self.model_config, device=device,
                                        build_model_ctx=build_model_ctx)  # 建「空格子」
    if not self.misc_config.empty_init:
        load_model_weights(patched_model, model_path, device=device)     # 灌权重 ★
    if adapters is not None:
        add_adapters(patched_model, adapters, ...)                       # LoRA 适配器（u10-l2）
    self.patched_model = patched_model
```

> 小知识：`empty_init` 是一种「只搭骨架不灌权重」的模式（用于 PD 分离的 decode 节点等场景，权重另有来源）。默认为 `False`，即正常灌权重。

#### 4.1.4 代码实践

**实践目标**：确认「`from_pretrained` 不碰权重、权重加载在 `_build_model`」这一分层。

**操作步骤**：

1. 打开 `lmdeploy/pytorch/engine/engine.py`，用编辑器搜索 `load_model_weights` 与 `ModelWeightLoader`——你会发现**搜不到**。这说明引擎主类确实把权重加载外包了。
2. 在同一文件搜索 `build_executor`，定位 `__init__` 里的调用。
3. 打开 `lmdeploy/pytorch/engine/model_agent/agent.py`，搜索 `load_model_weights`，定位到 `_build_model` 中的调用（约 1126 行）。

**需要观察的现象**：engine.py 中 `from_pretrained` → `__init__` → `build_executor` 是一条「只传递参数、不碰张量」的链；真正读 `.safetensors` 的代码在 model agent。

**预期结果**：你能画出 4.1.2 中那张调用链图，并指出「建空模型」与「灌权重」是 `_build_model` 中先后两行。

> 待本地验证：若本地已 `pip install -e .` 并有可用 GPU，可在 `_build_model` 的 `load_model_weights` 调用前各加一行 `logger.info(...)`（仅作阅读实验，勿提交），观察日志顺序确认「先建模型后灌权重」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Engine.from_pretrained` 里要有一个 `enable_mp_engine` 的分支？
> **参考答案**：多进程引擎（mp_engine）有独立的模型构建与权重更新流程（注释明确说它「has its own weight-update workflow」），不能用普通的 `cls(...)` 路径，因此提前分流到 `build_mp_engine`。

**练习 2**：如果设置 `empty_init=True`，`_build_model` 会跳过哪一步？后续权重从哪里来？
> **参考答案**：会跳过 `load_model_weights(patched_model, model_path, ...)`，即不读磁盘权重。后续权重由该节点自身的「权重更新流程」提供（典型场景是 PD 分离的 decode 节点接收 prefill 节点的运行时状态，而非重新加载）。

---

### 4.2 ModelWeightLoader：识别格式、枚举分片、产出迭代器

#### 4.2.1 概念说明

`ModelWeightLoader` 是本讲主角。它解决的问题是：**给定一个 HF 模型目录，把它抽象成「一个 `(名字, 张量)` 的迭代器」**，不管底层是 safetensors 还是 pytorch、是单文件还是分片。把「磁盘格式」这层差异屏蔽掉之后，下游的模型 `load_weights` 就只需面对统一的迭代器接口。

它的设计是经典的「**构造时识别 → 加载时迭代**」两段式：

- 构造（`__init__`）：扫描目录，判定 `weight_type` 与 `is_sharded`，算出所有分片文件路径 `self._shard_paths`。
- 加载（`load_model_weights`）：对每个分片文件，按格式选对应的迭代器函数，逐个 yield 出 `(name, tensor)`。

#### 4.2.2 核心流程

```
ModelWeightLoader(model_path)
   ├─ _get_weight_type(model_path)     # 判定格式 + 是否分片
   └─ _get_shard_paths(...)            # 得到所有分片文件路径 tuple

loader.load_model_weights(model, device)
   └─ for path in shard_paths:         # 逐分片
        weights_iterator = _get_weights_iterator(path)   # 选 safetensors / pytorch 迭代器
        weights_iterator = _rename_weights_iterator(...) # 改名（可选）
        weights_iterator = _skip_dummy_iterator(...)     # 跳过 dummy 模块（可选）
        model.load_weights(weights_iterator)             # 交给模型自己分配
```

注意：`ModelWeightLoader` **只负责「读出张量」和「按名迭代」**，它**不知道** `qkv_proj` 这类打包参数的存在——那是模型侧 `load_weights` 的事（4.3 节）。这种「读」与「分配」的分离，是本模块最重要的解耦。

#### 4.2.3 源码精读

**识别权重格式**：`_get_weight_type` 按优先级探测四个标志文件，返回 `(weight_type, is_sharded)`：

[model_weight_loader.py:38-59](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L38-L59) —— 用 `transformers.utils` 的常量名判定四种形态。

```python
def _get_weight_type(model_path, use_safetensors=None):
    if use_safetensors is not False and osp.isfile(... SAFE_WEIGHTS_NAME):        # 单 safetensors
        weight_type = 'safetensors'
    elif use_safetensors is not False and osp.isfile(... SAFE_WEIGHTS_INDEX_NAME):# 分片 safetensors
        weight_type, is_sharded = 'safetensors', True
    elif osp.isfile(... WEIGHTS_NAME):                                            # 单 pytorch bin
        weight_type = 'pytorch'
    elif osp.isfile(... WEIGHTS_INDEX_NAME):                                      # 分片 pytorch bin
        weight_type, is_sharded = 'pytorch', True
    ...
```

> `SAFE_WEIGHTS_NAME` = `model.safetensors`；`SAFE_WEIGHTS_INDEX_NAME` = `model.safetensors.index.json`；`WEIGHTS_NAME` = `pytorch_model.bin`；`WEIGHTS_INDEX_NAME` = `pytorch_model.bin.index.json`。safetensors 优先级高于 pytorch。

**从 index.json 读分片表**：分片模型有一个索引文件，里面 `weight_map` 记录「每个权重名字在哪个分片文件」：

[model_weight_loader.py:62-75](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L62-L75) —— 打开 index.json，取出 `weight_map`。

`_get_shard_paths` 对分片情况取 `weight_map.values()` 去重，得到所有分片文件路径：

[model_weight_loader.py:127-137](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L127-L137) —— 分片时取 `set(weight_map.values())`，单文件时退化为单元素 tuple。

**两种格式各自的迭代器**：这是屏蔽格式差异的关键。safetensors 用 `safe_open` 零拷贝读取；pytorch 用 `torch.load`：

[model_weight_loader.py:91-98](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L91-L98) —— safetensors 迭代器，逐 key 取张量，可选加前缀。

```python
def _get_safetensors_weights_iterator(file, prefix):
    with safe_open(file, framework='pt') as f:
        for name in f.keys():
            param = f.get_tensor(name)
            if prefix is not None:
                name = f'{prefix}{name}'
            yield name, param
```

[model_weight_loader.py:101-112](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L101-L112) —— pytorch 迭代器，`torch.load(..., weights_only=True)`，读完 `del state` 释放内存。

> `prefix` 的作用：对于 VLM 等模型，视觉塔权重可能整体加一个前缀（如 `language_model.`），以便与语言塔区分。

**逐分片加载的主循环**：`load_model_weights` 把上面几个积木串起来：

[model_weight_loader.py:162-189](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L162-L189) —— 核心循环：迭代器 → 改名 → 跳 dummy → 交给 `model.load_weights`。

```python
def load_model_weights(self, model, device=None):
    assert hasattr(model, 'load_weights')
    _, rank = get_world_rank()
    disable_tqdm = rank != 0                    # 只在 rank0 显示进度条
    dummy_prefix = [...]                        # 收集 dummy 模块前缀
    for path in tqdm(paths, desc='Loading weights from safetensors', disable=disable_tqdm):
        weights_iterator = self._get_weights_iterator(path)
        weights_iterator = self._rename_weights_iterator(weights_iterator, model)
        if len(dummy_prefix) > 0:
            weights_iterator = self._skip_dummy_iterator(weights_iterator, dummy_prefix)
        model.load_weights(weights_iterator)    # ★ 交给模型自己分配
    if device is not None:
        model.to(device)
```

**顶层模块函数 `load_model_weights`**：注意它和上面的**方法同名**——模块级函数是对外的便捷封装，构造 loader、调用方法、再 `update_weights` 收尾：

[model_weight_loader.py:192-201](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L192-L201) —— 顶层函数，包了 `@torch.inference_mode()`，加载后遍历所有模块调 `update_weights()`。

```python
@torch.inference_mode()
def load_model_weights(model, checkpoint_path, prefix=None, device=None):
    loader = ModelWeightLoader(checkpoint_path, prefix=prefix)
    loader.load_model_weights(model, device=device)
    model.eval()
    for _, mod in model.named_modules():
        if hasattr(mod, 'update_weights'):
            mod.update_weights()                # 加载后二次整理（见 4.4）
```

#### 4.2.4 代码实践

**实践目标**：亲手验证 `ModelWeightLoader` 对四种权重形态的识别。

**操作步骤**（纯源码阅读 + 本地 HF 目录观察，无需 GPU）：

1. 选一个本地 HF 模型目录（如已下载的 `Qwen/Qwen2.5-7B-Instruct`），用 `ls` 看它的权重文件：
   - 若有 `model.safetensors` → 单文件 safetensors；
   - 若有 `model.safetensors.index.json` + 多个 `model-0000X-of-0000Y.safetensors` → 分片 safetensors；
   - 若有 `pytorch_model.bin` → 单文件 pytorch；
   - 若有 `pytorch_model.bin.index.json` → 分片 pytorch。
2. 用 Python 复现识别逻辑（示例代码，**非项目原有代码**）：

   ```python
   # 示例代码：手动调用 _get_weight_type 复现识别
   from lmdeploy.pytorch.weight_loader.model_weight_loader import _get_weight_type
   print(_get_weight_type('/path/to/your/hf/model'))
   # 期待输出形如 ('safetensors', True)
   ```
3. 如果是分片模型，打开它的 `*.index.json`，找到 `weight_map` 字段，观察它如何把每个权重名映射到某个分片文件。

**需要观察的现象**：`_get_weight_type` 的返回值与你看到的文件一一对应；`weight_map` 里同一个分片文件名会出现很多次（因为一个分片装了很多权重）。

**预期结果**：你能解释「为什么 `ModelWeightLoader` 要在构造时就判定 `is_sharded`」——因为它决定了 `_get_shard_paths` 是去读 index 还是直接用单文件名。

> 待本地验证：步骤 2 的具体输出取决于你本地的模型；若没有本地模型，可只做步骤 1/3 的阅读理解。

#### 4.2.5 小练习与答案

**练习 1**：safetensors 和 pytorch 两种迭代器，哪个更省内存？为什么？
> **参考答案**：safetensors 更省。`_get_safetensors_weights_iterator` 用 `safe_open` 按需 `get_tensor`，可配合 mmap 零拷贝；而 `_get_pt_weights_iterator` 用 `torch.load` 一次性把整个分片反序列化进内存，读完后才 `del state` + `empty_cache`。所以大模型默认用 safetensors。

**练习 2**：为什么进度条 `tqdm` 要在 `rank != 0` 时禁用？
> **参考答案**：张量并行/数据并行时多个 rank 同时加载，若每个 rank 都打印进度条会互相覆盖、刷屏；只在 rank0 显示一条干净进度。

---

### 4.3 参数级加载契约：load_weight 与 weight_loader 属性

#### 4.3.1 概念说明

4.2 节的 `ModelWeightLoader` 只把张量「读出来」，**不知道** `qkv_proj` 这类打包参数该怎么切。真正「把张量塞进正确的参数」发生在模型的 `load_weights` 方法里（u3-l4 已见过 Llama 的版本）。本节讲清它和 loader 之间的**契约**：

> 模型拿到 `(name, tensor)` 后，先查自己的 `params_dict` 拿到目标 `param`，再调 `load_weight(param, tensor, **kwargs)`。`load_weight` 的工作是：**看这个 param 身上有没有挂 `weight_loader` 方法**，有就委托给它（处理打包与 TP 切片），没有就用默认的整块拷贝。

这个 `param.weight_loader` 属性是在线性层构造时由 `setup_loaders()` 挂上去的。它就是「loader 与 patched model 的对接点」。

#### 4.3.2 核心流程

以 Llama 加载一层 attention 的 q/k/v 为例：

```
HF 权重: model.layers.0.self_attn.q_proj.weight   (name)
   │  load_weights 里命中 stacked_params_mapping
   │  name.replace('.q_proj', '.qkv_proj')         # 改名到打包参数
   ▼
目标参数: model.layers.0.self_attn.qkv_proj.weight (一个拼接好的大矩阵)
   │  load_weight(param, tensor, shard_id='q')
   ▼
param.weight_loader(param, tensor, shard_id='q')    # 委托给线性层自己
   │  在 qkv 的大矩阵里定位 'q' 那一段
   │  按 TP 切出本 rank 需要的列
   ▼
default_weight_loader(param_slice, tensor_slice)    # 最终 copy_ 进显存
```

关键设计：**「改到哪个打包参数」由模型的 `stacked_params_mapping` 决定**；**「在打包参数里切哪一段、按 TP 切多少」由线性层的 `weight_loader` 方法决定**。两者分工明确。

#### 4.3.3 源码精读

**分派函数 `load_weight`**：本契约的「总入口」，只有几行，但定义了整个对接规则：

[model_weight_loader.py:19-25](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L19-L25) —— 优先委托 `param.weight_loader`，否则走默认拷贝。

```python
def load_weight(param, loaded_weight, **kwargs):
    if hasattr(param, 'weight_loader'):
        param.weight_loader(param, loaded_weight, **kwargs)   # 打包/TP 专用
    else:
        assert len(kwargs) == 0
        default_weight_loader(param, loaded_weight)           # 普通整块拷贝
```

**默认拷贝 `default_weight_loader`**：处理「普通」参数（没有打包、没有特殊 TP 逻辑），对齐形状后 `copy_`：

[model_weight_loader.py:28-35](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L28-L35) —— 标量用 `fill_`，张量断言同形状后 `copy_`。

```python
def default_weight_loader(param, loaded_weight):
    if param.numel() == 1 and loaded_weight.numel() == 1:
        param.data.fill_(loaded_weight.item())     # 标量（如 RMSNorm 的元素）
    else:
        assert param.size() == loaded_weight.size()
        param.data.copy_(loaded_weight)            # 整块拷贝
```

**模型侧如何使用契约——以 Llama 的 `load_weights` 为例**：先改名到打包参数，再带 `shard_id` 调 `load_weight`：

[llama.py:393-422](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L393-L422) —— `stacked_params_mapping` 描述「三个 HF 分片 → 一个打包参数」的映射。

```python
def load_weights(self, weights):
    stacked_params_mapping = [
        # (param_name, weight_name, shard_id)
        ('.qkv_proj', '.q_proj', 'q'),
        ('.qkv_proj', '.k_proj', 'k'),
        ('.qkv_proj', '.v_proj', 'v'),
        ('.gate_up_proj', '.gate_proj', 0),
        ('.gate_up_proj', '.up_proj', 1),
    ]
    params_dict = dict(self.named_parameters())
    for name, loaded_weight in weights:
        if 'rotary_emb.inv_freq' in name:           # 跳过不需要持久化的缓存
            continue
        ...
        for (param_name, weight_name, shard_id) in stacked_params_mapping:
            if weight_name not in name:
                continue
            name = name.replace(weight_name, param_name)  # q_proj -> qkv_proj
            param = params_dict[name]
            load_weight(param, loaded_weight, shard_id=shard_id)  # ★ 带 shard_id
            break
        else:
            param = params_dict[name]
            load_weight(param, loaded_weight)        # 普通参数，无 shard_id
```

> 这段「修改自 vllm」（注释明说）。`for...else` 的 `else` 在没有 `break`（即没命中任何打包映射）时执行，走普通分支。

**`weight_loader` 属性在哪里挂上去**：在线性层的 `setup_loaders` 里。以默认线性层为例：

[default.py:40-53](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/default.py#L40-L53) —— 把 `self.weight_loader` 方法挂到参数对象上当属性。

```python
def setup_loaders(self):
    self.weight.weight_loader = self.weight_loader   # ★ 挂钩
    if self.bias is not None:
        self.bias.weight_loader = self.weight_loader

def register_all_parameters(self, weight, bias=None):
    weight = torch.nn.Parameter(weight, requires_grad=False)
    ...
    self.register_parameter('weight', weight)
    self.setup_loaders()                              # 注册完参数立刻挂 loader
```

> 给 `torch.nn.Parameter` 动态挂属性是合法的——Parameter 本质是 Tensor 的子类对象，可以附加任意属性。`default_weight_loader` 和 awq 线性层在 4.3.2 流程图中是同一契约的两个实现。

**量化线性层的专用 loader**：以 AWQ 为例，它的权重是 `qweight/scales/qzeros` 三件套，每个都挂了 `weight_loader`，且该方法**接收 `shard_id`** 来定位 qkv 中的某一段：

[awq.py:238-245](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/awq.py#L238-L245) —— AWQ 的 `weight_loader` 用 `shard_id` 查表得到 `shard_idx`，再从打包参数里切对应段。

```python
def weight_loader(self, param, loaded_weight, shard_id):
    ...
    shard_idx = self.out_names_map[shard_id]          # 'q'->0, 'k'->1, 'v'->2
    ...
    param_w = param.data.split(self.all_out_features, 0)[shard_idx]  # 切出本段
    ...
```

挂载点同样在构造里：[awq.py:57-61](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/awq.py#L57-L61) —— `qweight/scales/qzeros` 各自挂 `weight_loader`。

#### 4.3.4 代码实践

**实践目标**：验证「`param.weight_loader` 属性 = loader 与模型的对接点」。

**操作步骤**（源码阅读型）：

1. 打开 `lmdeploy/pytorch/weight_loader/model_weight_loader.py`，读 `load_weight`（19-25 行）。确认它的分派逻辑：有 `weight_loader` 属性就委托，否则 `default_weight_loader`。
2. 打开 `lmdeploy/pytorch/nn/linear/default.py`，读 `setup_loaders`（40-44 行），确认 `self.weight.weight_loader = self.weight_loader` 这条赋值——这就是「挂钩」。
3. 对比 `lmdeploy/pytorch/nn/linear/awq.py` 的 `weight_loader`（238 行起）：它额外接收 `shard_id`，而默认线性层的 `weight_loader` 不需要。思考：为什么 AWQ 必须自己处理 shard？

**需要观察的现象**：默认线性层的 `weight_loader` 主要做 TP 切分（colwise/rowwise）；AWQ 的 `weight_loader` 既要处理 shard（q/k/v 段）又要处理量化布局（int4 打包后的形状与 `scales`/`qzeros`）。

**预期结果**：你能用一句话说清契约——「模型负责改名（`q_proj`→`qkv_proj`）和选 shard_id；线性层负责在自己这段参数里切对位置、按 TP 切对列；`load_weight` 只是个分派员」。

#### 4.3.5 小练习与答案

**练习 1**：如果一个参数既不在 `stacked_params_mapping` 里、`load_weight` 又发现它没有 `weight_loader` 属性，会发生什么？
> **参考答案**：走 `else` 分支，`len(kwargs)==0` 断言通过后调用 `default_weight_loader`，按形状断言一致后 `param.data.copy_(loaded_weight)` 整块拷贝。典型对象是 `norm.weight`、`embed_tokens.weight` 等。

**练习 2**：为什么 Llama 的 `load_weights` 要 `continue` 跳过 `rotary_emb.inv_freq` 这类名字？
> **参考答案**：`inv_freq`、`cos_cached`、`sin_cached` 是 rotary embedding 的**预计算缓存**，可由配置（头数、theta）现场算出，不必也不应从权重文件加载。patched model 里这些通常注册为 buffer 或运行时计算，所以读到的同名权重要跳过。

**练习 3**：`stacked_params_mapping` 里 gate/up 的 `shard_id` 是 `0/1`，而 q/k/v 的是 `'q'/'k'/'v'`，为什么类型不统一？
> **参考答案**：`shard_id` 只是「在打包参数里定位某一段」的标记，类型由对应线性层的 `weight_loader` 自定义。gate_up 用整数下标，qkv 用字符串 key（在 `out_names_map` 里查表），两者互不干扰——这正体现了「每个线性层用自己喜欢的方式解释 shard_id」的灵活性。

---

### 4.4 加载后的收尾：dummy 跳过、rename、update_weights 与推理模式

#### 4.4.1 概念说明

权重「灌进去」之后还有三件小事，散落在 4.2 的循环和 4.2 的顶层函数里，但语义独立，单独成节：

- **跳过 dummy 模块**：VLM 里常有「占位用的假模块」（`_is_dummy_mod=True`），它们的参数不该被权重覆盖。
- **`rename_weight` 改名**：少数模型（如 qwen3_omni、internvl3_hf）的 HF 权重名字与 LMDeploy 期望的不一致，需要在迭代时改名。
- **`update_weights` 二次整理**：灌完原始权重后，某些算子（如 blocked-fp8、MoE）需要根据刚灌进来的权重**重新计算一些派生量**（如反量化用的 scale、合并的 expert 权重）。
- **`@torch.inference_mode()` 与 `model.eval()`**：进入推理模式，关闭 autograd、关闭 dropout。

#### 4.4.2 核心流程

```
load_model_weights(model, path)              # 顶层函数，@torch.inference_mode
  ├─ ModelWeightLoader(path).load_model_weights(model)
  │     └─ for path in shards:
  │          iter ─► rename_weight ─► skip_dummy ─► model.load_weights(iter)
  ├─ model.eval()
  └─ for mod in named_modules():
        if hasattr(mod, 'update_weights'): mod.update_weights()   # 收尾
```

#### 4.4.3 源码精读

**改名迭代器**：从模型上读 `rename_weight` 方法（没有就用恒等函数 `lambda x: x`），对每个权重名做映射：

[model_weight_loader.py:154-160](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L154-L160) —— 包一层生成器，按模型自定义规则改名。

```python
@staticmethod
def _rename_weights_iterator(iterator, model):
    rename_func = getattr(model, 'rename_weight', lambda x: x)
    for name, param in iterator:
        new_name = rename_func(name)
        yield new_name, param
```

> 基类默认实现是「不改」：[model.py:56-59](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/utils/model.py#L56-L59) —— `rename_weight` 默认返回原名。需要改名的模型（如 `qwen3_vl`、`internvl3_hf`）才覆盖它。

**跳过 dummy 迭代器**：先把模型里所有 `_is_dummy_mod=True` 的子模块路径收集成前缀，再在迭代时跳过这些前缀开头的权重：

[model_weight_loader.py:147-152](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L147-L152) —— 用 `startswith` 跳过 dummy 前缀。

```python
@staticmethod
def _skip_dummy_iterator(iterator, dummy_prefix):
    for name, param in iterator:
        if not any(name.startswith(prefix) for prefix in dummy_prefix):
            yield name, param
```

`dummy_prefix` 在主循环里收集（4.2.3 已见）：`getattr(mod, '_is_dummy_mod', False)` 标记的模块（典型来源见 [model.py:187](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/utils/model.py#L187)）。

**`update_weights` 收尾**：顶层函数在加载完成后遍历所有模块，凡是有 `update_weights` 的就调用一次：

[model_weight_loader.py:198-201](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L198-L201) —— 收尾钩子，让各算子根据已加载权重做派生计算。

```python
for _, mod in model.named_modules():
    if not hasattr(mod, 'update_weights'):
        continue
    mod.update_weights()
```

> 基类默认空实现：[model.py:61-63](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/utils/model.py#L61-L63) —— 默认 `update_weights` 什么都不做。blocked_fp8、w8a8、MoE 等会覆盖它来重算反量化参数。这一步也是 LoRA 适配器加载后需要触发的（u10-l2）。

**`@torch.inference_mode()`**：[model_weight_loader.py:192](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L192) 给整个加载过程套上推理模式装饰器，确保加载时不构建计算图、不浪费显存。

#### 4.4.4 代码实践

**实践目标**：确认 `update_weights` 在加载后被调用，并理解它的「派生计算」用途。

**操作步骤**：

1. 在仓库里搜索哪些模块实现了 `update_weights`（前面已列出一批）：`grep -rn "def update_weights" lmdeploy/pytorch/nn/ lmdeploy/pytorch/models/`。
2. 任选一个量化线性层，如 `lmdeploy/pytorch/nn/linear/blocked_fp8.py` 的 `update_weights`（约 144 行），读它在「原始权重已 `copy_` 进来之后」又算了什么。典型情况是根据刚加载的 FP8 权重/尺度重排布局或预计算反量化系数。
3. 对比基类默认实现（`model.py:61-63` 的 `pass`），体会「需要派生计算的算子才覆盖它」的设计。

**需要观察的现象**：`update_weights` 出现在量化线性层（blocked_fp8/w8a8）、MoE 层和部分模型上；普通 FP16 线性层通常不需要（用基类的空实现）。

**预期结果**：你能解释「为什么加载不能止步于 `copy_`」——有些算子的运行时表示与磁盘格式不同，必须灌完原始权重后做一次性转换（避免每次 forward 都重算）。

> 待本地验证：若本地有 blocked-fp8 模型，可在其 `update_weights` 首行加 `logger.debug(...)`，确认它在 `load_model_weights` 主循环之后、模型可服务之前被调用一次。

#### 4.4.5 小练习与答案

**练习 1**：`rename_weight` 默认实现是什么？为什么大多数模型不需要覆盖它？
> **参考答案**：默认 `return name`（恒等）。因为多数模型的 HF 权重名与 LMDeploy 的 `MODULE_MAP` 目标类（如 `LlamaForCausalLM`）参数名已对齐；只有权重命名有特殊历史包袱的模型（internvl3_hf、qwen3_omni 等）才需要改名。

**练习 2**：`@torch.inference_mode()` 和 `model.eval()` 各自的作用？二者能互相替代吗？
> **参考答案**：`@torch.inference_mode()` 关闭 autograd 记录、降低开销，作用域是整个加载函数；`model.eval()` 把模块的 `training` 标志置 `False`，影响 dropout 和 BN 等模块行为。两者不可互替——前者管计算图/显存，后者管模块行为，加载推理模型时两个都要。

---

## 5. 综合实践

把本讲四节串起来，做一个「**端到端追踪一次权重加载**」的综合任务。

**任务**：假设有人问「我传给 pipeline 的模型路径，最终在哪一行代码被打开、被读进显存？」请你画出完整的源码路径并标注每一处的文件与行号。

**建议步骤**：

1. **起点**：`lmdeploy/pytorch/engine/engine.py` 的 `from_pretrained`（[L219](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L219)）→ `__init__` 中的 `build_executor`（[L152](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L152)）。
2. **中转**：`lmdeploy/pytorch/engine/model_agent/agent.py` 的 `_build_model`（[L1101](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py#L1101)），其中 `build_patched_model`（[L1123](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py#L1123)）建空模型、`load_model_weights`（[L1126](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py#L1126)）灌权重。
3. **真正打开文件**：`lmdeploy/pytorch/weight_loader/model_weight_loader.py`
   - 顶层函数 `load_model_weights`（[L193](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L193)）→ 构造 `ModelWeightLoader`（[L118](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L118)）。
   - 主循环 `load_model_weights` 方法（[L162](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L162)）→ 对每个分片取迭代器（safetensors 在 [L91](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L91)）→ 交给 `model.load_weights`（[L187](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L187)）。
4. **真正写进显存**：模型 `load_weights`（如 [llama.py:L393](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L393)）→ `load_weight`（[L19](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L19)）→ `param.weight_loader` 或 `default_weight_loader`（[L28](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L28)）里的 `param.data.copy_(...)`。

**交付物**：一张从「用户给的字符串路径」到「`param.data.copy_(loaded_weight)` 那一行」的完整调用链图，含每个文件与行号；并标注哪一步对应「识别格式」、哪一步对应「打包/TP 切分」、哪一步对应「收尾整理」。

> 进阶（可选）：若本地有 GPU 与已编译的 lmdeploy，开 `LMDEPLOY_LOG_LEVEL=DEBUG` 跑一次 `pipeline(...)`，从日志里找到 `build model.` 与 `loading weights.` 两条（来自 `_build_model` 的 `logger.debug`，[agent.py:L1110/L1124](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py#L1110-L1124)）以及 `Loading weights from safetensors` 进度条（[model_weight_loader.py:L182](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L182)），印证调用顺序。

## 6. 本讲小结

- **调用链**：`Engine.from_pretrained` → `__init__` → `build_executor` → `ModelAgent._build_model` → `build_patched_model`（建空模型）+ `load_model_weights`（灌权重）。引擎主类本身不碰权重。
- **`ModelWeightLoader`** 负责把 HF 目录抽象成统一的 `(name, tensor)` 迭代器：构造时识别 safetensors/pytorch × 单文件/分片 四种组合，加载时逐分片迭代，屏蔽磁盘格式差异。
- **参数级加载契约**：模型 `load_weights` 把名字映射到打包参数（`qkv_proj`/`gate_up_proj`）并给出 `shard_id`；`load_weight` 据此委托给 `param.weight_loader`（线性层在 `setup_loaders` 挂上），由后者处理 shard 定位与 TP 切分，否则走 `default_weight_loader` 整块拷贝。
- **三处收尾**：`_skip_dummy_iterator` 跳过占位模块、`_rename_weights_iterator` 按模型规则改名、顶层函数遍历模块调 `update_weights` 做派生计算；全程在 `@torch.inference_mode()` 下进行并以 `model.eval()` 收束。
- **safetensors 优于 pytorch bin**：可按需 `get_tensor`、内存更省；多 rank 加载只在 rank0 显示进度条。

## 7. 下一步学习建议

- **横向打通算子层**：本讲提到量化线性层的 `weight_loader` 自己处理 shard 与 TP 切分。建议下一站读 **u5-l2（线性层与权重量化变体）**，对照 `nn/linear/awq.py`、`w8a8.py`、`blocked_fp8.py` 的 `weight_loader` 实现，看不同量化格式如何在加载阶段就把磁盘上的 int4/int8 权重摆成 kernel 期望的布局。
- **纵向深入调度**：权重加载完成后，模型就进入「引擎执行与调度」。建议接 **u4-l1（Engine 主类与请求管理）**，看 `_build_model` 之后的 `build_graph_runner`、`build_cache_engine` 如何把灌好权重的模型接入持续批处理主循环。
- **LoRA 适配器**：本讲末尾提到 `_build_model` 里加载完基础权重后会 `add_adapters`，且 `update_weights` 是 LoRA 切换的触发点。可提前翻阅 **u10-l2（LoRA 适配器机制）** 了解适配器如何复用本讲的加载契约。
- **源码延伸阅读**：若想看 VLM 如何用 `rename_weight` 改名、用 `_is_dummy_mod` 跳过视觉塔占位，可挑 `lmdeploy/pytorch/models/qwen3_vl.py` 或 `internvl3_hf.py` 的 `load_weights`/`rename_weight` 对照阅读。

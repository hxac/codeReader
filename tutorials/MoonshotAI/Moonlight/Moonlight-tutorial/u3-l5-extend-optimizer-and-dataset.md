# 二次开发：扩展优化器与数据集

## 1. 本讲目标

本讲是学习手册的收官之作。前面十四讲我们把 `examples/toy_train.py` 从训练循环读到数据管线，又把 Muon 优化器的参数分组、Newton-Schulz 正交化、动量、权重衰减、RMS 缩放和 AdamW 后备分支逐一拆开。本讲换一个视角：**不再问"这段代码怎么工作"，而问"我想改它，应该从哪里下刀、改多大、怎么确认没改坏"**。

学完本讲你应该能够：

1. 在 `get_optimizer` 中新增一个自定义优化器分支（如 `hybrid`：注意力投影用 Muon、其余参数用 AdamW），且不修改 `Muon` 类与训练循环。
2. 在 `name2path` 中接入一个新的 HuggingFace 数据集，并处理好数据契约与 `.bin` 缓存。
3. 建立一套"基线先行、控制变量、参数计数断言、loss 轨迹对照"的回归验证习惯。
4. 学会评估改动的侵入半径：优先在既有扩展点上加分支，而不是复制文件或重写公共流程。

## 2. 前置知识

本讲综合运用前几讲的结论，这里只做要点复习，细节请回看对应讲义。

**参数分组是调用方的职责（u2-l1）**：`get_optimizer` 用"维度 ≥ 2 且名称不含 `embed_tokens`/`lm_head`"这个判据把参数分成两组，分别传给 `Muon` 的 `muon_params` 与 `adamw_params` 构造参数。`Muon` 类内部只用一个布尔标记 `state[p]["use_muon"]` 区分两条更新轨道。这意味着：**换一种分组策略，不需要动 `Muon` 类**。

**AdamW 后备分支能吃任意维度（u2-l5）**：`Muon.step` 的 AdamW backup 是逐元素自适应更新，一维 norm 向量、二维矩阵都能处理。所以把二维 FFN 矩阵划给 AdamW 组是合法的。

**更新 RMS 的量级差异（u2-l4、u3-l1）**：Muon 分支经 `0.2·√max(A,B)` 缩放后，更新均方根约为 `0.2η`；AdamW 分支约为 `η`。混合分组时这个差异会直接影响各参数的有效步长。

**数据管线的契约（u1-l4、u3-l2）**：`MoonDataset` 要求原始数据集有 `train` split 和 `text` 列；token 流缓存在 `{dataset_name}.bin`，**缓存键只含数据集名**——换数据集自动换缓存文件，但换分词器不会。

**模型与缓存的对齐（u3-l2）**：模型 `vocab_size=151936` 必须与 Qwen2 分词器一致；`max_position_embeddings=513` 限制了数据窗口不能超过 513。

**对比实验的方法论（u3-l1）**：控制变量、窗口平均 loss、对数网格学习率扫描。本讲的回归验证会复用这套仪表盘。

## 3. 本讲源码地图

本讲涉及的关键文件只有一个核心源码文件加一个说明文档：

| 文件 | 作用 | 本讲关注的区域 |
|---|---|---|
| [examples/toy_train.py](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py) | 全仓库唯一源码：数据集、Muon 优化器、模型装配、训练循环 | `get_optimizer`（L287-L313）、`name2path`（L243-L245）、`MoonDataset`（L16-L43）、`Muon.__init__`（L106-L140）、训练循环（L347-L359） |
| [README.md](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md) | 项目说明与训练命令 | Training 一节（L130-L137） |

两个改造点的定位直觉：

- **优化器扩展点**在 `get_optimizer` 的 `if/elif/else` 分发结构上，`else` 分支用 `assert 0` 快速失败——新优化器 = 新增一个 `elif`。
- **数据集扩展点**在 `name2path` 这张只有一项的别名表上——新数据集 = 新增一行映射。

## 4. 核心概念与源码讲解

本讲的四个最小模块：**新优化器接入**、**新数据集接入**、**回归验证方法**、**改动范围控制**。前两个是"往哪里加代码"，后两个是"加完怎么自证清白"。

### 4.1 新优化器接入：hybrid 分支

#### 4.1.1 概念说明

`toy_train.py` 目前支持两种优化器：`torch.optim.AdamW`（全参数）和 `Muon`（矩阵参数走 Muon、其余走内嵌 AdamW）。

本模块要新增第三种：**hybrid（混合）**——只把注意力投影矩阵 `q_proj/k_proj/v_proj/o_proj` 交给 Muon 正交化，其余参数（FFN 的 gate/up/down 投影、embedding、各 norm 向量）全部交给 AdamW。

为什么值得做这个实验？它回答一个源码无法直接回答的科学问题：**Muon 的收益是来自"所有矩阵都正交化"，还是主要来自注意力层的更新几何？**如果 hybrid 明显劣于纯 muon，说明 FFN 矩阵同样依赖正交化；如果两者接近，说明收益集中在注意力层。这类"消融"（ablation）正是论文实验设计的最小单元。

#### 4.1.2 核心流程

hybrid 分支的执行逻辑与纯 muon 分支完全同构，唯一差异是分组判据：

```
get_optimizer("hybrid", model, lr, wd)
  ├─ 遍历 model.named_parameters()
  ├─ 判据：参数名以 q_proj/k_proj/v_proj/o_proj 的 ".weight" 结尾？
  │    ├─ 是 → muon_params（4 × num_hidden_layers 个二维矩阵）
  │    └─ 否 → adamw_params（FFN 矩阵 + embedding + norm 向量）
  └─ return Muon(lr, wd, muon_params, adamw_params)
```

进入 `Muon` 之后，流程与 u2-l3/u2-l5 精读过的完全一致：

1. `__init__` 把两组参数合并进唯一的 param_group，逐参数打 `use_muon` 标记；
2. 每次 `step()` 按标记分流：Muon 组走"动量 → Newton-Schulz → 解耦衰减 → 缩放更新"，AdamW 组走"双动量 → 偏差校正 → 解耦衰减 → 更新"；
3. 训练循环的 `optimizer.step()` / 调度器 / 日志读取的 `param_groups[0]["lr"]` 全部无感兼容。

以默认 12 层模型为例（数字可用 4.1.4 的脚本核验）：

| | 纯 muon | hybrid |
|---|---|---|
| Muon 组 | 84（q/k/v/o + gate/up/down × 12 层） | 48（q/k/v/o × 12 层） |
| AdamW 组 | 26（embedding 1 + norm 25） | 62（FFN 36 + embedding 1 + norm 25） |
| 合计 | 110 | 110 |

#### 4.1.3 源码精读

先看扩展点本身——`get_optimizer` 的分发结构：

[examples/toy_train.py:287-313](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L287-L313)

```python
def get_optimizer(optimizer_name, model, lr=1e-3, wd=0.1):
    if optimizer_name == "adamw":
        return torch.optim.AdamW(
            model.parameters(), lr=lr, weight_decay=wd, betas=(0.9, 0.95)
        )
    elif optimizer_name == "muon":
        muon_params = [
            p
            for name, p in model.named_parameters()
            if p.ndim >= 2 and "embed_tokens" not in name and "lm_head" not in name
        ]
        ...
    else:
        assert 0, "optimizer not supported"
```

这段代码就是典型的**工厂函数 + 别名分发**模式：`optimizer_name` 字符串选分支，`else` 用 `assert 0` 快速失败，防止拼错的名字静默落到某个默认行为。要接入新优化器，只需在 `elif` 链上插入新分支——训练循环（L347-L359）对优化器唯一的接口约定是 `step()`、`param_groups` 和 `zero_grad()`，任何 `torch.optim.Optimizer` 子类都满足。

再看 `Muon.__init__` 为什么天然支持任意分组：

[examples/toy_train.py:129-140](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L129-L140)

```python
params = list(muon_params)
adamw_params = list(adamw_params) if adamw_params is not None else []
params.extend(adamw_params)
super().__init__(params, defaults)
for p in muon_params:
    assert p.ndim == 2, p.ndim
    self.state[p]["use_muon"] = True
for p in adamw_params:
    self.state[p]["use_muon"] = False
```

三个要点：

1. `muon_params` / `adamw_params` 是**构造参数**——分组策略完全由调用方决定，`Muon` 类不内置任何名称规则。这就是 hybrid 不用改类的根本原因。
2. `assert p.ndim == 2` 是唯一约束：Muon 组只能装二维矩阵。注意力投影 `q/k/v/o_proj.weight` 全是 `[hidden_size, hidden_size]`，天然满足；这个断言也让"误把一维参数划进 Muon 组"在构造时立刻崩溃，属于快速失败设计。
3. 两组用同一个 `use_muon` 布尔区分，`step()` 里按它过滤两次（L168 与 L209），互斥完备由调用方保证（u2-l1 讲过的补集写法）。

于是 hybrid 分支的完整改法如下（**示例代码**，非项目原有，需要你手动加入 `get_optimizer`）：

```python
elif optimizer_name == "hybrid":
    attn_projs = ("q_proj", "k_proj", "v_proj", "o_proj")

    def is_attn(name):
        return any(name.endswith(f"{proj}.weight") for proj in attn_projs)

    muon_params = [p for name, p in model.named_parameters() if is_attn(name)]
    adamw_params = [
        p for name, p in model.named_parameters() if not is_attn(name)
    ]
    return Muon(
        lr=lr,
        wd=wd,
        muon_params=muon_params,
        adamw_params=adamw_params,
    )
```

两个容易踩的判据陷阱，值得专门指出：

- **后缀必须带前缀字母**：如果图省事写 `name.endswith("proj.weight")`，那么 `gate_proj/up_proj/down_proj` 也会命中（它们都以 `proj.weight` 结尾），hybrid 就退化成"接近纯 muon"的分组，实验结论作废。必须写完整的 `f"{proj}.weight"`（如 `"q_proj.weight"`），它不会误伤 `gate_proj.weight`（后 13 个字符是 `ate_proj.weight`，不匹配）。
- **为什么不需要再排除 embed/lm_head**：`model.embed_tokens.weight` 不以任何注意力投影后缀结尾，自动落入 AdamW 组；而 `tie_word_embeddings=True` 下 `lm_head` 本就没有独立参数（u2-l1）。原判据里的名称排除项在 hybrid 语义下被后缀判据自然覆盖。

还有一个**学习率层面的诚实提醒**：hybrid 中 FFN 矩阵从 Muon 轨道（更新 RMS ≈ \(0.2\eta\)）搬到 AdamW 轨道（更新 RMS ≈ \(\eta\)），同一 `--lr` 下 FFN 的有效步长约为纯 muon 配置的 5 倍。而 `Muon.__init__` 的签名（L106-L117）**没有** `adamw_lr` 参数——尽管 docstring 在 [examples/toy_train.py:100-103](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L100-L103) 提到了它——两条轨道被硬性绑定在同一个 `lr` 上。所以 hybrid 的合理学习率区间可能与纯 muon 不同，对比时应像 u3-l1 那样先做小范围扫描，而不是直接沿用 `1e-3` 下结论。

#### 4.1.4 代码实践

**实践目标**：把 hybrid 分支加入 `get_optimizer`，并用参数计数证明分组正确、互斥、完备。

**操作步骤**：

1. 在 `get_optimizer` 的 `elif optimizer_name == "muon"` 分支之后、`else` 之前插入 4.1.3 的 hybrid 分支（示例代码）。
2. 写一个独立的检查脚本（**示例代码**，保存为 `check_hybrid.py` 放在仓库任意位置，跑完可删）：

   ```python
   import sys
   sys.path.insert(0, "examples")
   from toy_train import get_model_and_dataloader, get_optimizer

   model, _ = get_model_and_dataloader("qwen", "openwebtext-100k", 512)
   total = len(list(model.named_parameters()))

   opt = get_optimizer("hybrid", model)
   muon_group = [p for p in opt.param_groups[0]["params"] if opt.state[p]["use_muon"]]
   adamw_group = [p for p in opt.param_groups[0]["params"] if not opt.state[p]["use_muon"]]

   print(f"total={total}, muon={len(muon_group)}, adamw={len(adamw_group)}")
   assert len(muon_group) + len(adamw_group) == total, "分组不完备或有重复！"
   assert all(p.ndim == 2 for p in muon_group), "Muon 组混入非二维参数！"
   assert len(set(id(p) for p in muon_group + adamw_group)) == total, "参数被两组共享！"
   print("sample muon params:", [n for n, p in model.named_parameters() if id(p) in {id(q) for q in muon_group}][:4])
   ```

   注意 `get_model_and_dataloader` 会触发数据集下载与分词；如果只想查参数，本地已有 `.bin` 缓存时很快，否则可临时把 `MoonDataset` 那行注释掉再手工构造模型（待本地验证哪种更适合你的环境）。

3. 用 `python examples/toy_train.py --model qwen --optimizer hybrid --hidden_size 512 --lr 1e-3` 启动训练，跑几十步确认 loss 正常下降即可中断。

**需要观察的现象**：

- 检查脚本输出 `total=110, muon=48, adamw=62`（hidden_size 只改形状不改数量；若你按 u3-l2 改了层数，按 `4×层数` 与总数自行换算）。
- 训练第一步的 loss 接近 \( \ln 151936 \approx 11.93 \)（随机初始化的语言模型在词表上的理论值，u1-l2 讲过）。
- 日志文件自动命名为 `logs/train_qwen_hybrid_lr0.001.log`（L327 的命名模板含优化器名），与基线日志天然隔离。

**预期结果**：hybrid 能正常训练，loss 从约 11.9 起步并稳定下降；它在短窗口内与纯 muon/纯 adamw 的相对优劣属于实验结果，**待本地验证**——这正是综合实践（第 5 节）要回答的问题。

#### 4.1.5 小练习与答案

**练习 1**：hybrid 分支的判据为什么不需要像纯 muon 分支那样写 `"embed_tokens" not in name`？

**答案**：纯 muon 分支的判据是"维度够高就正交化"，因此要显式排除名称像嵌入/输出层的二维矩阵；hybrid 改用白名单式后缀判据（只认 `q/k/v/o_proj.weight`），`model.embed_tokens.weight` 不匹配任何后缀，自动落入 AdamW 组。白名单判据天然免疫"漏排除"。

**练习 2**：如果把判据写成 `p.ndim >= 2 and is_attn(name)`，对本模型是错是对？为什么？

**答案**：对本模型结果相同（注意力投影本来就都是二维），写法也对未来更稳——若某天注意力出现非二维参数，`ndim >= 2` 会把它排除出 Muon 组，避免触发 `__init__` 的 `assert p.ndim == 2`。防御性写法的成本只是多一个条件。

**练习 3**：为什么 hybrid 完全不需要修改 `Muon` 类？

**答案**：`Muon` 的分组策略是经 `muon_params`/`adamw_params` 构造参数从外部注入的，类内部只认 `use_muon` 布尔标记（L134-L140）。hybrid 只是换了一组传入的参数列表，接口未变；这也验证了 u2-l1 的结论——"谁该正交化"是策略，"怎么正交化"才是 `Muon` 的职责。

### 4.2 新数据集接入：name2path 加一项

#### 4.2.1 概念说明

`toy_train.py` 目前只认一个数据集别名 `openwebtext-100k`。本模块要接入第二个 HuggingFace 数据集，并弄清 `MoonDataset` 对数据格式的不成文契约——接入新数据集时，真正的工作量往往不在"加一行映射"，而在核对契约。

选型上我们用 `roneneldan/TinyStories`（合成儿童故事语料，约 2M 篇短文）做示范：它体量小、无需额外 config、自带 `train` split 和 `text` 列，与 `MoonDataset` 的期望结构完全对齐，是玩具实验的理想第二数据集。

#### 4.2.2 核心流程

新数据集进入训练的完整链路：

```
--dataset tinystories
  → name2path["tinystories"] = "roneneldan/TinyStories"
  → load_dataset("roneneldan/TinyStories", trust_remote_code=True)
  → 契约核对：有 "train" split？有 "text" 列？
  → MoonDataset：逐篇分词 → 拼接成 token 长流 → 缓存 tinystories.bin
  → 按 max_length=512 切块 → DataLoader(batch_size=16)
```

缓存的行为规则（u1-l4 的结论在本讲的应用）：缓存文件名是 `f"{self.dataset_name}.bin"`，键里只有数据集别名——**换数据集 = 新缓存文件，互不污染；换分词器 = 旧缓存仍被复用，必须手动删**。本讲不换分词器（Qwen2Tokenizer 硬编码在 L248-L250），所以无需清理 `openwebtext-100k.bin`。

#### 4.2.3 源码精读

先看扩展点——别名表只有一项：

[examples/toy_train.py:243-246](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L243-L246)

```python
name2path = {
    "openwebtext-100k": "Elriggs/openwebtext-100k",
}
train_dataset = load_dataset(name2path[dataset_name], trust_remote_code=True)
```

`name2path` 是"命令行别名 → HuggingFace 仓库 ID"的翻译层；`load_dataset` 直接拿翻译结果当数据集路径。注意 `load_dataset` 的第二个位置参数是 dataset config 名——本调用没传，意味着接入的数据集要么无需 config，要么你得把 config 写进映射值。接入方式（**示例代码**，一行改动）：

```python
name2path = {
    "openwebtext-100k": "Elriggs/openwebtext-100k",
    "tinystories": "roneneldan/TinyStories",
}
```

再看 `MoonDataset` 对数据集的两条硬契约：

[examples/toy_train.py:21](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L21)

```python
self.texts = dataset["train"]["text"]
```

契约一：必须存在 `train` split；契约二：必须存在名为 `text` 的列。`roneneldan/TinyStories` 两条都满足。若你相中的数据集列名不同（如 `content` 或 `sentence`），最小侵入的适配不是改 `MoonDataset`，而是在 `load_dataset` 之后加一行重命名（**示例代码**）：

```python
train_dataset = train_dataset.rename_column("content", "text")
```

第三条隐含契约在缓存读写处：

[examples/toy_train.py:26-33](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L26-L33)

```python
def _tokenize_texts(self):
    if os.path.exists(f"{self.dataset_name}.bin"):
        self.tokens = torch.load(f"{self.dataset_name}.bin")
    else:
        for text in tqdm(self.texts, desc="Tokenizing texts"):
            encoded = self.tokenizer.encode(text, add_special_tokens=True)
            self.tokens.extend(encoded)
        torch.save(self.tokens, f"{self.dataset_name}.bin")
```

`dataset_name` 直接充当缓存键。接入 TinyStories 后首次运行会走 `else` 分支逐篇分词并写出 `tinystories.bin`；二次运行命中 `os.path.exists` 直接 `torch.load`。唯一需要留意的边界：如果你在中断首次分词后再启动，会从零重来（没有断点续存），且若曾经手工创建过同名残缺 `.bin`，会静默加载坏缓存——删掉重来即可。

最后确认模型侧约束没有被破坏：`MoonDataset` 默认 `max_length=512`，构造调用（[examples/toy_train.py:253](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L253)）未传该参数，窗口仍是 512 ≤ `max_position_embeddings=513`（u3-l2），换数据集不触碰这条约束；`len(self.tokens) // max_length`（L36）意味着 TinyStories 的 token 总量决定 `len(train_loader)`，进而决定 cosine 调度的总步数（[examples/toy_train.py:341-346](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L341-L346)）——不同数据集的学习率曲线形状不同，对比 loss 时要记得这一层差异（u1-l4 讲过的归因问题）。

#### 4.2.4 代码实践

**实践目标**：接入 TinyStories 并跑通一次训练，体会"首次分词慢、二次启动快"的缓存行为。

**操作步骤**：

1. 在 `name2path` 中加入 `"tinystories": "roneneldan/TinyStories"`（示例代码，见 4.2.3）。
2. 首次运行（分词约需数分钟量级，视机器而定）：

   ```bash
   python3 examples/toy_train.py --model qwen --optimizer adamw \
       --dataset tinystories --hidden_size 512 --lr 1e-3
   ```

3. 观察到 `Tokenizing texts` 进度条走完后训练开始，中断之；再次运行同一命令。
4. （可选）在 `logs/` 下确认两份日志；注意日志文件名模板（L327）**不含数据集名**，两次运行写的是同一个 `train_qwen_adamw_lr0.001.log`，且 `logger.add` 默认追加。

**需要观察的现象**：

- 首次运行出现 `Tokenizing texts` 进度条，项目根目录生成 `tinystories.bin`（与 `openwebtext-100k.bin` 并存）。
- 二次运行跳过分词，几乎立即进入训练日志输出。
- 训练前几步 loss 从约 11.9 下行；TinyStories 文本简单、重复度高，loss 下降速度预期快于 openwebtext（**待本地验证**）。

**预期结果**：三条全部满足即接入成功。若报 `KeyError: 'text'` 或 `train` 相关错误，说明所选数据集不满足 4.2.3 的契约，按重命名/换 split 的方向修。

#### 4.2.5 小练习与答案

**练习 1**：为什么接入新数据集不需要删除 `openwebtext-100k.bin`，而（按 u1-l4）换分词器却必须删缓存？

**答案**：缓存键是 `f"{dataset_name}.bin"`，只含数据集别名。换数据集 → 新别名 → 新缓存文件，互不干扰；换分词器 → 别名不变 → 旧的 Qwen2 token 流被 `torch.load` 直接复用，模型却在用另一套词表，token id 与 vocab 错位会静默出错。

**练习 2**：目标数据集只有 `content` 列和 `validation` split，各需要什么适配？

**答案**：列名问题用 `train_dataset = train_dataset.rename_column("content", "text")`（或 `remove_columns` 后统一字段）解决；split 问题二选一——在 `load_dataset` 后改用 `dataset["validation"]`（需把 `MoonDataset` L21 的 `"train"` 参数化，侵入稍大），或用 `datasets` 的拆分功能从该 split 切出训练份额。两种都要同步考虑缓存重建。

**练习 3**：`load_dataset(name2path[dataset_name], trust_remote_code=True)` 中，若数据集需要 config 名（如维基百科的语言版本），如何最小侵入地支持？

**答案**：利用 `datasets` 对 `"仓库名:config名"` 语法的支持，把映射值写成整串，如 `"wikimedia/wikipedia:20231101.simple"`，`name2path` 与调用行都不用改；或把映射值改为 `(path, config)` 元组并改调用为 `load_dataset(path, config, ...)`。前者改动最小（**待本地验证**所用 datasets 版本对该语法的支持情况）。

### 4.3 回归验证方法：证明"没改坏"

#### 4.3.1 概念说明

二次开发最大的风险不是"改了报错"，而是"改了还能跑，但行为已经错了"——参数被漏掉、被两组重复更新、缓存串味，这些错误在 loss 曲线上可能只是"略微变差"，肉眼难辨。回归验证（regression testing）的思路是：**在改动之前先固化基线，改动之后在完全相同的条件下重跑，用可量化的指标对照**。

本模块给出一套针对 `toy_train.py` 的四层验证清单，成本从低到高：

1. **静态层：参数计数断言**（秒级）——分组互斥完备性的代数证明。
2. **启动层：首步 loss 合理性**（分钟级）——初始 loss 应接近 \( \ln(\text{vocab\_size}) \)。
3. **轨迹层：窗口平均 loss 对照**（小时级以内）——与基线在前 N 步的下降形态对比。
4. **语义层：抽样人工检查**——打印若干样本 token id，确认在 `[0, vocab_size)` 内且解码回可读文本。

#### 4.3.2 核心流程

一个完整的回归验证循环：

```
① 冻结基线
   git checkout -b baseline && 跑 N 步 → 保存日志 → git commit（或记录 commit hash）
② 施加改动
   git checkout -b feature → 在 get_optimizer/name2path 加分支
③ 静态断言
   运行参数计数脚本（4.1.4）→ 断言通过
④ 短程对照
   同模型、同数据、同 lr、同步数 → 新日志
⑤ 轨迹对比
   解析两份日志 → 同窗口移动平均 → 绘图/列表
⑥ 结论与记录
   差异在噪声内？超参需重扫？记录实验卡片
```

两个容易忽略的统计事实：其一，脚本**没有设置随机种子**（全篇找不到 `torch.manual_seed`），`shuffle=True` 的 DataLoader 与随机初始化使每次运行的 loss 有天然抖动，单次对比要用窗口平均（u3-l1 的方法）而非逐步对比；其二，L327 的 `logger.add` 是**追加模式**且文件名不含数据集名与步数，多组实验后同一文件里混着多段运行，解析时要按时间取最后一段或每组实验前清空日志。

#### 4.3.3 源码精读

验证清单的每一层都锚定在源码的具体行为上。

**第一层锚点：分组互斥完备**。纯 muon 分支用补集写法保证完备（u2-l1 精读过），hybrid 改为两个独立判据后，这层保证就要靠自己验证：

[examples/toy_train.py:293-304](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L293-L304)

```python
muon_params = [
    p
    for name, p in model.named_parameters()
    if p.ndim >= 2 and "embed_tokens" not in name and "lm_head" not in name
]
adamw_params = [
    p
    for name, p in model.named_parameters()
    if not (
        p.ndim >= 2 and "embed_tokens" not in name and "lm_head" not in name
    )
]
```

原代码里 AdamW 组的判据恰好是 Muon 组判据整体取反——一个参数不可能同时属于两组（不会双重更新），也不可能两组都不属于（不会静默冻结）。若你的新分支用两个独立判据（如 4.1.3 的 `is_attn` 与 `not is_attn`），保持"同一谓词取反"的写法，或干脆用 4.1.4 脚本里的三条断言：总数相等、无重复、Muon 组全二维。

**第二层锚点：首步 loss 的理论值**。日志输出在：

[examples/toy_train.py:357-359](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L357-L359)

```python
logger.info(
    f"Epoch: {epoch} Step: {step} LR: {optimizer.param_groups[0]['lr']} Training loss: {loss.item()}"
)
```

随机初始化的模型在 151936 词表上给出的首步 loss 理论值为 \( \ln 151936 \approx 11.93 \)。它是一个免费的"冒烟测试"：改动后首步 loss 若显著偏离（例如冲到几十、上百万或 NaN），优先排查数据侧（token id 越界、缓存串味）与模型侧（vocab_size 与分词器错位，u3-l2）。

**第三层锚点：每步 token 量恒定**。窗口平均 loss 之间可比的前提是每步看到的 token 数相同——由 `MoonDataset` 的定长切块与 DataLoader 的 batch_size 共同保证：

[examples/toy_train.py:35-36](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L35-L36)

```python
def __len__(self):
    return len(self.tokens) // self.max_length
```

与 [examples/toy_train.py:254](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L254) 的 `DataLoader(train_dataset, batch_size=16, shuffle=True)` 合起来，每步固定消耗 16 × 512 = 8192 个 token（u3-l1 用它做"阈值到达步数"效率度量的依据）。回归对比时保持 `--hidden_size`、`--dataset`、`--lr` 全部相同，唯一变量是优化器分支。

**第四层锚点：被日志掩盖的一个既有事实**。调用优化器构造时没有转发 `--wd`：

[examples/toy_train.py:332-334](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L332-L334)

```python
optimizer = get_optimizer(
    args.optimizer, model, lr=args.lr
)
```

u1-l2 已确认：无论命令行传什么 `--wd`，`get_optimizer` 的 `wd` 形参都用默认值 0.1。做回归对比时这反而是好事——权重衰减被意外固定，少了一个自由度；但写实验报告时应如实注明"所有运行 wd=0.1（命令行参数未生效）"。顺带一提，README（[README.md:130-137](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L130-L137)）给出的两条训练命令是官方口径的基线复现入口，回归实验可直接以它们为参照系。

#### 4.3.4 代码实践

**实践目标**：为 hybrid 改动建立可复现的短程回归对照（adamw vs muon vs hybrid 三线）。

**操作步骤**：

1. 在改动前的 `master` 上跑两个基线（可后台运行，各跑约 200-300 步后手动中断，脚本没有 max_steps 参数）：

   ```bash
   python3 examples/toy_train.py --model qwen --optimizer adamw  --hidden_size 512 --lr 1e-3
   python3 examples/toy_train.py --model qwen --optimizer muon   --hidden_size 512 --lr 1e-3
   ```

2. 记下当时的 `git rev-parse HEAD`；应用 4.1.3 的 hybrid 改动（可顺手在文件顶部加注释说明改动意图与日期）。
3. 运行 4.1.4 的计数脚本，三条断言全过。
4. 跑 `--optimizer hybrid` 同样步数。三份日志分别是 `train_qwen_{adamw,muon,hybrid}_lr0.001.log`。
5. 用一个十行脚本（**示例代码**）解析日志并做窗口平均：

   ```python
   import re
   from collections import deque

   def curve(path, window=50):
       losses = []
       with open(path) as f:
           for line in f:
               m = re.search(r"Training loss: ([\d.]+)", line)
               if m:
                   losses.append(float(m.group(1)))
       return [sum(losses[i-window:i])/window for i in range(window, len(losses)+1, window)]

   for name in ["adamw", "muon", "hybrid"]:
       pts = curve(f"logs/train_qwen_{name}_lr0.001.log")
       print(name, [f"{x:.3f}" for x in pts])
   ```

   注意追加写入问题：同一文件若混有多段运行，先按"最后一段"截取或实验前清理旧日志。

**需要观察的现象**：

- 三条曲线的起点都略低于/接近 11.93 并单调（带噪声）下行；
- muon 曲线相对 adamw 的位置（u3-l1 的结论：短程内差异可能不大，扫描 lr 后才见分晓）；
- hybrid 落在哪里——介于两者之间、贴近 muon，还是贴近 adamw。

**预期结果**：三线均正常下降即回归通过；hybrid 与两条基线的相对位置属于**待本地验证**的实验结论。若 hybrid 明显劣于 adamw，结合 4.1.3 的学习率分析，先怀疑 FFN 矩阵在 AdamW 轨道的有效步长偏大，换更小的 `--lr` 重试再下结论。

#### 4.3.5 小练习与答案

**练习 1**：为什么必须"先跑基线、后改代码"，而不是改完一起跑？

**答案**：两个原因。可复现性：基线绑定改动前的 commit hash，任何时候都能 `git checkout` 回去重跑；环境漂移：依赖升级、数据集远程更新都可能混入差异，先跑基线把环境状态与基线锁定在一起，之后的对比才能把差异归因于代码改动本身。

**练习 2**：改动后训练正常、loss 也在降，但参数计数脚本报"分组不完备"。最可能发生了什么？

**答案**：某个参数既不满足 Muon 判据也不满足 AdamW 判据（或反之被两组同时命中）。此时训练不会崩溃——漏掉的参数保持随机初始化（或被重复更新两次），loss 照样下降，但模型已是"半残"状态。这正是参数计数断言的价值：它是唯一能抓住这类静默错误的廉价检查。

**练习 3**：首步 loss 是 15.7 而不是约 11.9，最值得优先检查什么？

**答案**：数据与词表的对齐。首步 loss 由初始权重分布与词表大小决定，理论上应紧贴 \( \ln 151936 \)。显著偏高常见于：`.bin` 缓存来自另一套分词器（token id 分布与当前模型 embedding 不匹配）、或 token id 越界触发了框架的边界行为。按"删缓存重分词 → 核对 vocab_size"的顺序排查。

### 4.4 改动范围控制：最小侵入原则

#### 4.4.1 概念说明

同样的功能可以有很多种改法，侵入半径天差地别。本讲的两处改动都刻意选择了最小半径：

- hybrid：**加一个 `elif` 分支**（约 12 行，全部位于 `get_optimizer` 内）；
- TinyStories：**加一行映射**（1 行，位于 `name2path`）。

作为对照，两种"大半径"方案——复制整个脚本为 `toy_train_hybrid.py`，或把 `MoonDataset` 改成支持任意列名的通用类——都能实现同样的功能，但会把仓库变成两份需要同步维护的事实源。最小侵入不是教条，而是一笔账：**改动半径越大，与上游（本仓库后续更新）的合并冲突越多，回归验证需要覆盖的面也越大**。

判断半径的一条实用准则：先找代码已有的"接缝"（工厂分支、别名表、构造参数），优先沿接缝扩展；接缝不存在时再评估开新接缝的成本。

#### 4.4.2 核心流程

一次受控改动的检查单：

```
① 定位接缝：这次改动落在哪个既有扩展点？
② 评估触达面：
   - 会改公共流程吗？（训练循环、MoonDataset、Muon 类 → 高风险）
   - 会改默认行为吗？（默认参数、既有分支 → 向后兼容问题）
   - 会改共享状态吗？（.bin 缓存、日志文件 → 实验间串扰）
③ 加失败保护：新分支的非法输入要像 else 一样快速报错
④ 记录改动清单：哪些文件、哪些行、为什么
⑤ 跑 4.3 的回归验证
⑥ 提交为独立 commit，写清动机（方便回滚与 review）
```

#### 4.4.3 源码精读

`toy_train.py` 里三处设计正面示范了"接缝在哪"，我们逐个反推它们的意图。

**接缝一：`else` 分支的快速失败**。

[examples/toy_train.py:312-313](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L312-L313)

```python
else:
    assert 0, "optimizer not supported"
```

拼错优化器名不会静默回退到某个默认优化器，而是在启动一秒内崩溃。hybrid 分支插在它之前，保住了这层防御——任何新 typo 仍然会被兜住。这个模式在模型选择处也出现（[examples/toy_train.py:251-252](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L251-L252) 的 `assert 0, f"model {model_name} not supported"`），是全脚本一致的错误处理风格：**新代码应当沿用，而不是用 `print` + 默认值吞掉错误**。

**接缝二：`name2path` 是数据、代码之间唯一的耦合点**。

[examples/toy_train.py:243-245](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L243-L245)

```python
name2path = {
    "openwebtext-100k": "Elriggs/openwebtext-100k",
}
```

数据集的"仓库 ID"这个易变信息被收拢在一张表里，`MoonDataset`、`DataLoader`、分词逻辑都不感知具体数据集。代价是这张表埋在函数体内，无法从命令行枚举可选值——若要继续演进，可把它提为模块级常量并用 `choices=name2path.keys()` 约束 argparse，但那是另一个量级的重构，对本讲目标属于过度设计。

**接缝三：构造参数即扩展协议**。`Muon.__init__` 的签名（[examples/toy_train.py:106-117](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L106-L117)）暴露 `muon_params`/`adamw_params`，把分组策略让渡给调用方。反例也在同处：docstring（[examples/toy_train.py:100-103](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L100-L103)）承诺了 `adamw_lr`、`adamw_wd` 两个参数，签名里却不存在。它提醒我们：**文档声明的接口未必是真实接口**，二次开发前以签名与调用点为准。如果你确实需要给 AdamW 组单独的学习率，改动清单会显著变长——`defaults` 增键、`param_groups` 拆组、`step()` 的 AdamW 段读组内 lr、调度器对多组的改写行为核验——这正是一个"评估半径后再动手"的练习（见 4.4.5）。

顺带一个向后兼容的观察：`--optimizer` 的默认值是 `adamw`（[examples/toy_train.py:321](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L321)），新增 hybrid 分支不触碰任何默认行为——不传新名字的用户完全无感，这是"加分支"天然优于"改既有分支"的又一理由。

#### 4.4.4 代码实践

**实践目标**：为你的改动写一份"改动清单 + 风险自查"，养成先评估后动手的习惯。

**操作步骤**：

1. 应用 4.1.3 与 4.2.3 的两处改动后，运行 `git diff examples/toy_train.py`，逐行核对。
2. 用下面的模板（**示例模板**）写一份 `Moonlight-tutorial/` 之外的本地笔记（或实验记录），确认每一项都想清楚了：

   | 项目 | 本讲改动的答案 |
   |---|---|
   | 改动文件 / 行数 | `examples/toy_train.py` / 约 13 行 |
   | 触达的既有函数 | `get_optimizer`（加分支）、`get_model_and_dataloader`（加映射） |
   | 是否改公共流程（训练循环 / MoonDataset / Muon 类） | 否 |
   | 是否改变默认行为（默认参数 / 既有分支） | 否（`--optimizer` 默认仍为 adamw） |
   | 是否引入共享状态风险 | 否（新缓存文件独立；日志文件按优化器名区分，但按数据集会追加混写） |
   | 非法输入的失败方式 | 沿用 `else: assert 0`；`name2path` 未命中会 KeyError（也是快速失败） |
   | 回归验证状态 | 参数计数断言通过 + 三线短程对照已跑 |

3. 把改动提交为独立 commit（例如 `git add -p` 只挑这两个函数的改动），commit message 写明"新增 hybrid 优化器分支与 TinyStories 数据集别名"。

**需要观察的现象**：`git diff` 中不应出现 `MoonDataset`、`Muon` 类、训练循环的任何改动；若出现了，停下来问自己为什么需要动它们。

**预期结果**：改动收敛在两个扩展点内，回归验证四层检查全过，且 `master` 上旧行为（不传新参数的旧命令）逐字节不变——**待本地验证**：改动后重跑一条 README 原始命令（如 [README.md:136](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L136)），前几十步 loss 应与你改动前的同命令日志一致（同种子不可得，只比形态与量级）。

#### 4.4.5 小练习与答案

**练习 1**：为什么不建议复制一份 `toy_train_hybrid.py` 来做实验？什么情况下复制反而是对的？

**答案**：复制会产生第二份训练循环、数据管线的事实源，上游修复或你自己的改进要同步两处，长期必然漂移；且实验结论写在"分叉版"上，与主线结果不可直接对照。复制只在改动本质上是"另一个实验框架"（例如要重写整个训练循环做 DDP，如 u3-l4 的实践）时才合理，而且应明确标注为独立实验品。

**练习 2**：评估"给 AdamW 组单独设置学习率"的改动半径。

**答案**：至少四处：`Muon.__init__` 的 `defaults` 与签名要加 `adamw_lr`（顺带修复 docstring 与签名的不一致）；两组学习率不同意味着要么拆成两个 param_group、要么在 `step()` 的 AdamW 段（L205-L237）改读 `group.get("adamw_lr", group["lr"])`；调度器 `LambdaLR` 只改写 param_group 的 `lr`，单独的 adamw_lr 不会被 warmup/cosine 缩放，需要决定它是否参与调度；回归验证还要覆盖"两 lr 组合"的新维度。对比之下，接受"两轨道共用 lr"（u2-l5 说明过其可行性依赖 RMS 一致化）是半径小得多的现状。

**练习 3**：hybrid 分支的判据如果硬编码 `"q_proj.weight" in name` 这种包含式写法，风险是什么？

**答案**：包含式匹配对名称全集做子串测试，依赖命名空间里恰好没有歧义；一旦上游（transformers 的 Qwen2 实现）改名或新增含该子串的参数，行为悄悄改变。后缀匹配（`endswith`）+ 快速失败断言的组合把假设显式化：不匹配预期形态的参数宁可进 AdamW 组（安全侧），计数脚本再兜底。

## 5. 综合实践

把本讲四个模块串成一个完整的二次开发闭环：**"接入 hybrid 优化器 + 接入 TinyStories 数据集 + 三基线回归对照"**。

### 5.1 任务描述

在 `examples/toy_train.py` 上完成两处最小侵入改动，然后回答一个开放实验问题：

> 在小模型、短训练窗口下，hybrid（仅注意力正交化）与纯 muon、纯 adamw 相比，loss 轨迹处于什么位置？

### 5.2 操作步骤

1. **冻结基线**（4.3）：记录当前 commit；在 `master` 上用 `--hidden_size 512 --lr 1e-3` 分别跑 `adamw` 与 `muon` 各约 200-300 步，保存两份日志。
2. **施加改动**（4.1、4.2）：加 hybrid 分支与 `tinystories` 别名，各一个独立 commit。
3. **静态验证**（4.1.4）：参数计数断言三条全过，记录 `48 / 62 / 110`（12 层模型）。
4. **数据验证**（4.2.4）：跑通一次 `--dataset tinystories`，确认 `tinystories.bin` 生成、二次启动跳过分词。
5. **三线对照**（4.3.4）：在 `openwebtext-100k` 上跑 hybrid 同步数；用窗口平均脚本输出三线对照表。
6. **（选做）鲁棒性检查**：把 `--lr` 降到 `5e-4` 再各跑一次，观察 hybrid 的相对位置是否随学习率改变（呼应 4.1.3 的"FFN 有效步长 ×5"分析）。
7. **实验卡片**：用 4.4.4 的模板记录改动清单、命令、环境（GPU 型号 / torch 版本）、曲线对照表与结论。

### 5.3 观察点与预期

- 三线都从约 11.93 起步、正常下降 → 回归通过。
- hybrid 的相对位置与是否随 lr 变化 → **待本地验证**；无论结果如何，把它与"FFN 矩阵在 AdamW 轨道有效步长更大"的假设对照，写进实验卡片。
- 若中途任何一步首步 loss 异常或计数断言失败 → 回到 4.3 的分层排查清单。

### 5.4 交付物

1. 两处改动的 diff（各一个 commit）。
2. 参数计数脚本的输出。
3. 三线（或六线，含选做）窗口平均 loss 对照表。
4. 一张实验卡片：结论、限制（单规模、无种子、短窗口——u3-l1 的告诫同样适用）、下一步想验证什么。

## 6. 本讲小结

- `get_optimizer` 的 `if/elif + assert 0` 分发结构与 `name2path` 别名表是仓库预留的两个天然扩展点；hybrid 优化器与新数据集各用"一个分支 / 一行映射"即可接入，训练循环、`MoonDataset`、`Muon` 类零改动。
- `Muon` 类经 `muon_params`/`adamw_params` 构造参数把分组策略让渡给调用方，`use_muon` 布尔标记在 `step()` 内分流——换分组只需换传入的参数列表；唯一硬约束是 Muon 组必须全二维（`__init__` 断言）。
- 接入新数据集的真正工作量在核对 `MoonDataset` 的两条契约（`train` split、`text` 列）与缓存规则（键只含数据集名：换数据集天然隔离，换分词器必须手删 `.bin`）。
- 回归验证四层清单：参数计数断言（互斥完备）→ 首步 loss ≈ ln(vocab) 冒烟测试 → 窗口平均 loss 三线对照 → 样本语义抽查；脚本无随机种子、日志追加写入，是对照时最容易踩的两个统计坑。
- 最小侵入是一笔账：先沿既有接缝扩展、不改默认行为、非法输入快速失败、改动清单化；hybrid 与纯 muon 的 RMS 量级差异（0.2η 对 η）提醒我们——接口没变不等于数值行为没变，对比实验前先想清楚有效步长。

## 7. 下一步学习建议

本讲是手册最后一讲，`toy_train.py` 的每一行你都读过了。三个方向继续深入：

1. **向论文与工业实现对齐**：通读 `Moonlight.pdf` 的实验章节与 scaling law 拟合，再看 [README.md](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md) 链接的 Megatron-LM PR #1428，对照 u3-l4 的 ZeRO-1 分析，理解本讲 toy 版与分布式版的差距（状态分片、通信重叠、混合精度）。
2. **回到 Muon 上游**：toy_train.py 的 Muon 改编自 KellerJordan/Muon（源码注释 [examples/toy_train.py:46-47](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L46-L47) 标明了出处），去上游看社区最新的系数调法、speedrun 记录与讨论，能反过来加深对 u2 各讲的理解。
3. **把综合实践做大**：在 u3-l4 的 DDP 版本上重跑本讲的 hybrid 对照；或按 u3-l2 缩出一个六层小模型，把三线对照扩成"模型规模 × 优化器"的小网格——你就在亲手做一个微缩版的 scaling law 实验，这正是 Moonlight 论文方法论的起点。

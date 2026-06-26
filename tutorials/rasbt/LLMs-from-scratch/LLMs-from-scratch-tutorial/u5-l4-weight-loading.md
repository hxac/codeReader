# 权重保存/加载与加载 OpenAI GPT-2 权重

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 PyTorch 的 `state_dict` 把训练好的 `GPTModel` 权重保存到磁盘，并在一个全新的模型实例里精确还原它。
- 理解为什么要把「模型权重 + 优化器状态」一起打包进 checkpoint，以便日后断点续训。
- 读懂 `gpt_download.py` 如何从 OpenAI 下载 TensorFlow 格式的 GPT-2 权重、并用 `tf.train` 把它解析成 Python 字典。
- 读懂 `load_weights_into_gpt` 如何把 OpenAI 的权重「逐层、逐张量」搬进我们手写的 `GPTModel`，并理解其中转置（`.T`）与权重绑定（weight tying）的来龙去脉。
- 亲手加载 `gpt2-small (124M)` 权重，用 `Every effort moves you` 生成一段连贯文本，从而「用结果验证权重确实加载对了」。

本讲是第 5 章预训练的收尾：u5-l2 训出来的模型因为训练语料太小而过拟合，u5-l3 只是让生成更多样。真正让这个「从零搭的 GPT」变得有用的，是本讲——直接把 OpenAI 在海量语料上预训练好的权重搬进来。

## 2. 前置知识

本讲默认你已经掌握：

- **u4-l3 的 `GPTModel` 与 `GPT_CONFIG_124M`**：知道模型由 `tok_emb`、`pos_emb`、12 个 `trf_blocks`、`final_norm`、`out_head` 组成；也知道「直接统计参数量是 163M，扣掉 `out_head` 才是 124M」这件事与权重绑定有关——本讲会把这个坑填上。
- **u5-l3 的 `generate` 函数**：知道温度采样、top-k 采样与自回归循环。本讲复用它来验证加载结果。
- **u1-l3 的两种代码复用模式**：本讲的两个脚本正好是一对活教材——`gpt_download.py` 是「可被 import 的模块」，`gpt_generate.py` 是「自包含、单文件可跑」的汇总脚本（把下载逻辑又内联了一遍）。
- **PyTorch 基础**：`nn.Module`、`Parameter`、`nn.Linear` 的权重形状约定是 `[out_features, in_features]`（这一点对理解本讲的 `.T` 至关重要）。

几个本讲会反复出现的新术语，先给出直觉解释：

| 术语 | 直觉解释 |
|------|----------|
| **state_dict** | 一个「参数层名 → 张量」的有序字典，是 PyTorch 序列化模型的「标准打包格式」。 |
| **checkpoint** | 训练过程中存到磁盘的「存档点」，通常不止含权重，还含优化器状态等。 |
| **`weights_only=True`** | `torch.load` 的安全开关，只允许反序列化纯张量，拒绝执行任意代码（防恶意 `.pth`）。 |
| **`map_location`** | 加载时把张量「搬到」哪个设备（如 CPU 上存的、GPU 上加载）。 |
| **权重绑定（weight tying）** | 让「输入端嵌入矩阵」与「输出端分类头」共用同一份权重，GPT-2 即如此。 |
| **Conv1D vs Linear 的布局差异** | OpenAI GPT-2 用 TensorFlow 的 `Conv1D` 存权重为 `[in, out]`，而 PyTorch `nn.Linear` 是 `[out, in]`，二者互为转置。 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ch05/01_main-chapter-code/ch05.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb) | 正文 notebook。§5.4 演示 `state_dict` 的保存/加载；§5.5 调用下载与映射函数，把 OpenAI 权重灌进模型并生成文本。 |
| [ch05/01_main-chapter-code/gpt_download.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_download.py) | 可被 import 的下载模块。notebook 里 `from gpt_download import download_and_load_gpt2` 用的就是它，含主/备双下载源与 TF checkpoint 解析。 |
| [ch05/01_main-chapter-code/gpt_generate.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py) | 自包含的命令行汇总脚本：内联了下载逻辑、`assign`、`load_weights_into_gpt`、`generate` 与 `main`，单文件即可「下载→加载→生成」。 |
| [ch05/01_main-chapter-code/previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/previous_chapters.py) | 提供 `GPTModel`。本讲反复确认它暴露的属性名（`tok_emb`/`pos_emb`/`trf_blocks[b].att.W_query`/`ff.layers[0]`/`norm1.scale` 等）与映射函数一一对应。 |

---

## 4. 核心概念与源码讲解

### 4.1 state_dict：PyTorch 的标准权重持久化

#### 4.1.1 概念说明

训练一个 LLM 极其昂贵（书里举的例子：Llama 2 7B 要 18 万 GPU·小时、约 69 万美元）。所以「把训练成果存盘、以后还能原样取回」是刚需。

PyTorch 推荐的做法不是存「整个模型对象」，而是存 **`state_dict`**——一个把每一层参数按名字登记好的字典。这样做有三个好处：

1. **解耦结构与权重**：存盘文件里只有张量，不含类定义。加载时你必须先 `new` 一个**结构完全相同**的空模型，再把张量「灌」进去。这让权重文件小巧、且跨代码版本更健壮。
2. **可审计**：`state_dict` 的键就是属性访问路径（如 `trf_blocks.0.att.W_query.weight`），一目了然。
3. **便于增量**：可以额外把优化器状态也塞进同一个文件，实现断点续训。

#### 4.1.2 核心流程

「只存权重」的最小流程：

```text
保存: model.state_dict() ──torch.save──> "model.pth"
加载: GPTModel(同配置) ──load_state_dict(torch.load("model.pth"))──> 还原
```

「存权重 + 优化器状态」的断点续训流程：

```text
保存: torch.save({"model_state_dict":..., "optimizer_state_dict":...}, "model_and_optimizer.pth")
加载: checkpoint = torch.load(...)
      model.load_state_dict(checkpoint["model_state_dict"])
      optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
```

> 为什么连优化器也要存？因为 Adam/AdamW 这类**自适应优化器**会为每个权重额外维护一阶动量、二阶动量等状态。如果只恢复权重、不恢复优化器状态，续训时优化器就像「失忆」了一样，动量从零重新累积，训练曲线会出现抖动。

#### 4.1.3 源码精读

notebook §5.4 给出了教科书式的保存单行（[ch05.ipynb:2087](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb#L2087)）：

```python
torch.save(model.state_dict(), "model.pth")
```

`.pth` 只是约定俗成的扩展名（PyTorch 历史遗留），底层 `torch.save` 用的是 Python 的 `pickle` 把字典序列化成二进制。

加载时则要先重建模型、再灌权重（[ch05.ipynb:2119-2120](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb#L2119-L2120)）：

```python
model = GPTModel(GPT_CONFIG_124M)
model.load_state_dict(torch.load("model.pth", map_location=device, weights_only=True))
model.eval();
```

两个关键参数：

- **`weights_only=True`**：从 PyTorch 2.0 起强烈推荐。`pickle` 反序列化原则上能执行任意代码，加载来历不明的 `.pth` 有安全风险；该开关限制只允许加载张量数据，堵住这条攻击面。本项目里所有 `torch.load` 都带上了它。
- **`map_location=device`**：把张量重定向到目标设备。比如权重是在 GPU 上存的、现在想在 CPU 上推理，就需要它来「跨设备搬运」。

断点续训的保存（[ch05.ipynb:2139-2143](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb#L2139-L2143)）：

```python
torch.save({
    "model_state_dict": model.state_dict(),
    "optimizer_state_dict": optimizer.state_dict(),
    }, 
    "model_and_optimizer.pth"
)
```

对应的加载（[ch05.ipynb:2154-2160](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/ch05.ipynb#L2154-L2160)）：

```python
checkpoint = torch.load("model_and_optimizer.pth", weights_only=True)
model = GPTModel(GPT_CONFIG_124M)
model.load_state_dict(checkpoint["model_state_dict"])

optimizer = torch.optim.AdamW(model.parameters(), lr=0.0005, weight_decay=0.1)
optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
model.train();
```

注意 `optimizer` 必须用**同一批 `model.parameters()`** 重新构造，再把存档的状态灌进去——优化器状态是按参数顺序索引的，参数顺序对不上就会出错。

#### 4.1.4 代码实践

**实践目标**：亲手验证「存盘 → 还原」是无损的——还原后的模型在同样输入下应产出**逐位相同**的 logits。

**操作步骤**（接在 u5-l2 训练完的 `model` 之后，或任意一个 `GPTModel` 实例之后）：

1. 用 u5-l3 的 `text_to_token_ids` 准备一段固定输入。
2. `model.eval()` 后跑一次前向，记录输出 logits（建议取前几个值即可）。
3. `torch.save(model.state_dict(), "model.pth")` 存盘。
4. 新建 `model2 = GPTModel(GPT_CONFIG_124M)`，`load_state_dict` 灌入，`model2.eval()`。
5. 对 `model2` 跑同样的前向。

**需要观察的现象**：两次 logits 是否完全相等。

**预期结果**：

```python
torch.allclose(out1, out2)  # → True
```

> 待本地验证：具体数值取决于你训练到的权重，但「两次完全一致」这一点是确定的。如果你看到不相等，最常见原因是忘了 `eval()`（dropout 随机激活）或两次 `GPT_CONFIG_124M` 不一致。

#### 4.1.5 小练习与答案

**练习 1**：如果把权重存到 `.pth` 后，故意用一个 `n_layers=6`（而非 12）的 `GPTModel` 去 `load_state_dict`，会发生什么？

**参考答案**：会报错。`load_state_dict` 默认要求存档的键集合与目标模型的键集合**完全一致**；少了 6 个 `trf_blocks` 的参数对不上，会抛出 `RuntimeError`（unexpected/missing keys）。这正体现了「加载时结构必须与保存时相同」。

**练习 2**：为什么断点续训要连 `optimizer.state_dict()` 一起存，而纯推理部署时不需要？

**参考答案**：推理阶段只做前向、不再更新权重，优化器根本用不上，存它纯属浪费空间。只有「还要继续训练」时，AdamW 的一阶/二阶动量才需要被恢复，否则续训初期会因动量丢失而抖动。

---

### 4.2 下载并解析 OpenAI GPT-2 权重（gpt_download）

#### 4.2.1 概念说明

我们手写的 `GPTModel` 在结构上和 OpenAI 当年发布的 GPT-2 是一致的——这正是 u4 那几讲精心对齐的结果。既然结构相同，我们就可以「白嫖」OpenAI 在海量网页语料上花重金预训练出的权重，把它直接灌进自己的模型，瞬间获得一个会说话的 GPT-2。

但有个工程障碍：**OpenAI 当年是用 TensorFlow 训练的**，发布的权重是 TensorFlow checkpoint 格式（`model.ckpt.*` 一组文件），而不是 PyTorch 的 `.pth`。所以本节要做两件事：

1. **下载**那一组 TF 文件（外加 `encoder.json`、`vocab.bpe` 这两个分词器文件、`hparams.json` 超参文件）。
2. **解析**：借助 TensorFlow 读 checkpoint，把里面的变量重组成 Python 字典，供下一节的映射函数使用。

> 这也是为什么 `requirements.txt` 里要列 `tensorflow`——它在整个项目里**只用于这一步解析权重**，不参与任何训练或推理。

#### 4.2.2 核心流程

`download_and_load_gpt2(model_size, models_dir)` 的流程（[gpt_download.py:16-45](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_download.py#L16-L45)）：

```text
1. 校验 model_size ∈ {124M, 355M, 774M, 1558M}
2. 对 7 个文件逐个下载（主源失败 → 备用源）到 models_dir/<size>/
3. tf.train.latest_checkpoint 找到 checkpoint 路径
4. 读 hparams.json 得到 settings（n_layer、n_head 等）
5. load_gpt2_params_from_tf_ckpt：遍历 checkpoint 里所有变量，重组成 params 字典
6. 返回 (settings, params)
```

`load_gpt2_params_from_tf_ckpt` 的核心是把 TF 的「斜杠分层命名」翻译成嵌套字典（[gpt_download.py:126-152](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_download.py#L126-L152)）：

```text
TF 变量名: model/h0/attn/c_attn/w
            ↓ split("/")[1:] 去掉 "model/" 前缀
        ["h0", "attn", "c_attn", "w"]
            ↓ h0 以 "h" 开头 → 第 0 层
        params["blocks"][0]
            ↓ 中间键逐层 setdefault
        params["blocks"][0]["attn"]["c_attn"]
            ↓ 末键赋值
        params["blocks"][0]["attn"]["c_attn"]["w"] = <numpy 数组>
```

非层级的全局变量（`model/wte`、`model/wpe`、`model/g`、`model/b`）则因为首段不以 `h` 开头，直接挂到 `params` 根上。最终 notebook 打印出的 `params.keys()` 正是 `['blocks', 'b', 'g', 'wpe', 'wte']`，`settings` 是 `{'n_vocab': 50257, 'n_ctx': 1024, 'n_embd': 768, 'n_head': 12, 'n_layer': 12}`。

#### 4.2.3 源码精读

入口函数先做尺寸校验与路径准备（[gpt_download.py:16-38](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_download.py#L16-L38)）：

```python
allowed_sizes = ("124M", "355M", "774M", "1558M")
...
base_url = "https://openaipublic.blob.core.windows.net/gpt-2/models"
backup_base_url = "https://f001.backblazeb2.com/file/LLMs-from-scratch/gpt2"
filenames = [
    "checkpoint", "encoder.json", "hparams.json",
    "model.ckpt.data-00000-of-00001", "model.ckpt.index",
    "model.ckpt.meta", "vocab.bpe"
]
```

注意 `gpt_download.py` 比 `gpt_generate.py` 里内联的同名函数**多了一个 `backup_base_url`**：当 OpenAI 官方源（Azure blob）连不上时，自动回退到作者自建的 Backblaze 镜像。这正是 notebook 选择 `from gpt_download import ...` 而不直接用脚本内联版的原因——模块版更健壮。这也呼应了 u1-l3 的两种复用模式：模块版为「可复用、可加固」，脚本版为「自包含、易分发」。

解析函数最关键的一段（[gpt_download.py:131-150](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_download.py#L131-L150)）：

```python
for name, _ in tf.train.list_variables(ckpt_path):
    variable_array = np.squeeze(tf.train.load_variable(ckpt_path, name))
    variable_name_parts = name.split("/")[1:]            # 去掉 'model/' 前缀

    target_dict = params
    if variable_name_parts[0].startswith("h"):
        layer_number = int(variable_name_parts[0][1:])   # "h0" -> 0
        target_dict = params["blocks"][layer_number]

    for key in variable_name_parts[1:-1]:                # 中间键逐层下钻
        target_dict = target_dict.setdefault(key, {})

    last_key = variable_name_parts[-1]
    target_dict[last_key] = variable_array               # 末键落盘
```

两个细节值得记住：

- **`np.squeeze`**：TF checkpoint 里有些变量带多余的长度为 1 的维度（如 `[1, 768, 2304]`），`squeeze` 把这些「假维度」压掉，得到干净的 `[768, 2304]`。
- **`setdefault(key, {})`**：下钻时若中间字典不存在就自动创建，省去一堆 `if key not in ...`。这是构建任意深度嵌套字典的惯用技巧。

> 一个工程小贴士：notebook 在本节开头特意提示——某些 Windows 环境装 TensorFlow 会出兼容性问题。若你遇到，可改用 `../02_alternative_weight_loading/` 里**已经转好成 PyTorch `.pth`** 的权重，直接 `load_state_dict`，绕开 TensorFlow。两条路殊途同归。

#### 4.2.4 代码实践

**实践目标**：不急着映射，先把下载下来的 `params` 字典「摸清楚」，建立对 OpenAI 权重布局的直觉。

**操作步骤**：

```python
from gpt_download import download_and_load_gpt2
settings, params = download_and_load_gpt2(model_size="124M", models_dir="gpt2")

print("Settings:", settings)
print("Top keys:", params.keys())
print("wte shape:", params["wte"].shape)              # 词嵌入
print("block0 keys:", params["blocks"][0].keys())     # 一层里有哪些子模块
print("c_attn w shape:", params["blocks"][0]["attn"]["c_attn"]["w"].shape)
```

**需要观察的现象**：

- `settings` 应给出 `n_layer=12, n_head=12, n_embd=768, n_ctx=1024`。
- `params["wte"].shape` 应为 `(50257, 768)`——词表 50257、嵌入维 768。
- `params["blocks"][0]` 应含 `attn`、`mlp`、`ln_1`、`ln_2` 四个子字典。
- `c_attn["w"].shape` 应为 `(768, 2304)`——这是把 Q、K、V **三份权重拼在一起**存的结果（768×3=2304），下一节正是沿最后一维三等分。

**预期结果**：上述形状与说明一致（待本地验证数值，但形状由模型规模唯一确定）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `c_attn["w"]` 的形状是 `(768, 2304)` 而不是三份独立的 `(768, 768)`？

**参考答案**：OpenAI GPT-2 把 Q、K、V 三个投影合并成一个 `c_attn` 层（TF 的 `Conv1D`），权重按 `[in, 3*out]` 拼接存放，便于一次矩阵乘同时算出 Q/K/V。映射时再用 `np.split(..., 3, axis=-1)` 把它沿最后一维拆回三份。

**练习 2**：`download_and_load_gpt2` 返回的 `settings` 里哪个字段，决定了下一节 `for b in range(...)` 要循环几层？

**参考答案**：`settings["n_layer"]`（=12）。`load_gpt2_params_from_tf_ckpt` 正是用它来预分配 `params["blocks"]` 列表长度的（[gpt_download.py:128](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_download.py#L128)）。

---

### 4.3 load_weights_into_gpt：逐层权重映射

#### 4.3.1 概念说明

上一节得到的 `params` 字典用的是 **OpenAI / TensorFlow 的命名与布局**，而我们 `GPTModel` 里的参数用的是 **PyTorch 的命名与布局**。两者名字不同、个别张量形状还互为转置。`load_weights_into_gpt` 就是这个「翻译官」：它一条一条地把 `params` 里的张量搬进 `GPTModel` 对应的属性。

翻译过程中要处理三类「不对齐」：

1. **命名不对齐**：OpenAI 叫 `wte`/`wpe`/`c_attn`/`c_proj`/`c_fc`/`ln_1`/`g`/`b`，我们叫 `tok_emb`/`pos_emb`/`W_query`/`out_proj`/`ff.layers[0]`/`norm1`/`scale`/`shift`。
2. **布局不对齐（转置）**：OpenAI 的 `Conv1D` 权重是 `[in, out]`，PyTorch `nn.Linear` 权重是 `[out, in]`，所以要 `.T`。
3. **QKV 合并 vs 拆分**：OpenAI 一份 `c_attn` 含 Q/K/V，我们要拆给三个独立的 `W_query`/`W_key`/`W_value`。

此外还有本讲呼应 u4-l3 的关键一笔——**权重绑定**：最后用同一份 `wte` 同时充当输出头 `out_head` 的权重。

#### 4.3.2 核心流程

`load_weights_into_gpt(gpt, params)`（[gpt_generate.py:126-184](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L126-L184)）的整体顺序：

```text
1. 嵌入层:    pos_emb ← wpe,  tok_emb ← wte
2. 逐层循环 (b = 0..11):
   2a. 注意力: c_attn 拆三份 + 转置 → W_query/W_key/W_value (权重与偏置各一份)
              c_proj + 转置 → out_proj
   2b. 前馈:   c_fc  + 转置 → ff.layers[0],  c_proj + 转置 → ff.layers[2]
   2c. 归一化: ln_1 → norm1.scale/shift,  ln_2 → norm2.scale/shift
3. 收尾:      final_norm ← g/b,  out_head ← wte   ← 这一步即「权重绑定」
```

每一步搬运都经过同一个「安检」函数 `assign`（[gpt_generate.py:120-123](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L120-L123)）：

```python
def assign(left, right):
    if left.shape != right.shape:
        raise ValueError(f"Shape mismatch. Left: {left.shape}, Right: {right.shape}")
    return torch.nn.Parameter(torch.tensor(right))
```

它的作用是：**先校验两边形状一致**（形状对不上直接报错，避免静默错位），再把 numpy 数组包成 `nn.Parameter` 返回，由调用处赋给模型属性。这种「形状即契约」的设计，让你一旦写错映射顺序就会立刻得到清晰报错，而不是生成出乱码后才回头排查。

#### 4.3.3 源码精读

**嵌入层**——位置嵌入 `wpe`、词嵌入 `wte` 直接搬（[gpt_generate.py:127-128](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L127-L128)）：

```python
gpt.pos_emb.weight = assign(gpt.pos_emb.weight, params["wpe"])
gpt.tok_emb.weight = assign(gpt.tok_emb.weight, params["wte"])
```

**注意力的 QKV 拆分与转置**——本讲最关键的一步（[gpt_generate.py:131-138](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L131-L138)）：

```python
q_w, k_w, v_w = np.split(
    (params["blocks"][b]["attn"]["c_attn"])["w"], 3, axis=-1)
gpt.trf_blocks[b].att.W_query.weight = assign(
    gpt.trf_blocks[b].att.W_query.weight, q_w.T)
```

`np.split(..., 3, axis=-1)` 把 `(768, 2304)` 沿最后一维切成三份 `(768, 768)`，分别对应 Q、K、V。每个 `.T` 把 `[in, out]` 转成 PyTorch 要的 `[out, in]`。注意 `trf_blocks` 虽是 `nn.Sequential`，但它支持下标索引，所以 `trf_blocks[b]` 能直接取到第 b 个 `TransformerBlock`，再 `.att` 取到其中的 `MultiHeadAttention`——这条属性链必须与 `previous_chapters.py` 里的命名逐字吻合（u4-l2 已确认过这些名字）。

QKV 的**偏置**同理三等分（无需转置，因为偏置是一维的，[gpt_generate.py:140-147](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L140-L147)）。

> 这里也解释了为什么加载 OpenAI 权重时必须把配置改成 `"qkv_bias": True`：OpenAI 的 GPT-2 当年在 Q/K/V 线性层**带偏置**，我们的模型要与之对齐才能正确接收这些偏置张量（否则 `W_query.bias` 根本不存在，`assign` 会失败）。

**前馈网络**——`c_fc` 对应升维层、`c_proj` 对应降维层（[gpt_generate.py:156-167](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L156-L167)）：

```python
gpt.trf_blocks[b].ff.layers[0].weight = assign(
    gpt.trf_blocks[b].ff.layers[0].weight,
    params["blocks"][b]["mlp"]["c_fc"]["w"].T)
...
gpt.trf_blocks[b].ff.layers[2].weight = assign(
    gpt.trf_blocks[b].ff.layers[2].weight,
    params["blocks"][b]["mlp"]["c_proj"]["w"].T)
```

`ff.layers` 是个 `nn.Sequential`，`layers[0]` 是升维 `Linear(768→3072)`、`layers[1]` 是 `GELU`、`layers[2]` 是降维 `Linear(3072→768)`。注意跳过了 `layers[1]`——因为 GELU 没有可学习参数。

**层归一化**——OpenAI 的 `ln_1`/`ln_2` 各有增益 `g` 与偏移 `b`，对应我们的 `norm1`/`norm2` 的 `scale`/`shift`（[gpt_generate.py:169-180](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L169-L180)）。这正是 u4-l1 里 `LayerNorm` 自定义的 `scale`/`shift` 两个 `nn.Parameter`。

**收尾：最终归一化 + 权重绑定**（[gpt_generate.py:182-184](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L182-L184)）：

```python
gpt.final_norm.scale = assign(gpt.final_norm.scale, params["g"])
gpt.final_norm.shift = assign(gpt.final_norm.shift, params["b"])
gpt.out_head.weight = assign(gpt.out_head.weight, params["wte"])
```

最后一行是点睛之笔：输出头 `out_head` 直接复用词嵌入矩阵 `wte`。这就是 **weight tying（权重绑定）**——u4-l3 留的那个坑。其几何含义是：词嵌入把「token ID」编码成向量，输出头把向量解码回「词表上的分数」，二者天然互逆，理应共享同一组向量：

\[ \text{logits} = h \, W_{\text{emb}}^{\top}, \qquad W_{\text{emb}} \in \mathbb{R}^{|V| \times d} \]

即用嵌入矩阵的转置充当输出投影。原始 GPT-2 如此设计既省参数（`out_head` 不再独立，这正是「163M 扣掉 out_head 才是 124M」的口径），又让编解码空间对称。严格说，`assign` 这里是**把 `wte` 的值复制**一份给 `out_head`（两个独立的 `Parameter` 对象、初值相同），而非共享存储；但对「加载预训练权重做推理」而言效果一致。

> **如何判断权重真加载对了？** 书里的判据非常朴素而有力（见 notebook 末尾）：让模型生成一段文本，如果输出**连贯可读**，就说明逐层映射全对了；哪怕只错一个转置或漏一层，输出都会瞬间退化成乱码。下一节的综合实践就是用这条判据验收。

#### 4.3.4 代码实践

**实践目标**：单独追踪「一层注意力的 QKV 权重」从 `params` 到模型属性的完整变形，把转置与拆分这两个最容易出错的地方亲手走一遍。

**操作步骤**（接 4.2.4 已下载的 `params`）：

```python
import numpy as np
import torch
from previous_chapters import GPTModel

# 1. 取出 OpenAI 第 0 层合并的 c_attn 权重
c_attn_w = params["blocks"][0]["attn"]["c_attn"]["w"]   # (768, 2304)
print("c_attn w:", c_attn_w.shape)

# 2. 沿最后一维三等分
q_w, k_w, v_w = np.split(c_attn_w, 3, axis=-1)
print("拆分后:", q_w.shape, k_w.shape, v_w.shape)        # 各 (768, 768)

# 3. 转置成 PyTorch Linear 的 [out, in] 布局
q_w_for_torch = q_w.T
print("转置后:", q_w_for_torch.shape)                    # (768, 768)

# 4. 构建一个加载好权重的模型，核对第 0 层 W_query 是否与之逐元素相等
cfg = {"vocab_size":50257,"context_length":1024,"emb_dim":768,
       "n_heads":12,"n_layers":12,"drop_rate":0.0,"qkv_bias":True}
# （省略 load_weights_into_gpt(gpt, params) 的调用，见综合实践）
```

**需要观察的现象**：拆分后三份形状一致、转置后维度顺序翻转；加载完成的模型里 `gpt.trf_blocks[0].att.W_query.weight` 应与 `q_w_for_torch` 数值相同。

**预期结果**：

```python
torch.allclose(gpt.trf_blocks[0].att.W_query.weight,
               torch.tensor(q_w_for_torch))   # → True （待本地验证）
```

#### 4.3.5 小练习与答案

**练习 1**：如果把 `q_w.T` 误写成 `q_w`（忘了转置），`assign` 会报错吗？模型还能正常生成吗？

**参考答案**：对本例不会报错——因为 `q_w` 与 `q_w.T` 形状都是 `(768, 768)`，`assign` 的形状校验拦不住。但权重的「输入/输出维度」被颠倒了，注意力计算会用到一组语义错乱的投影矩阵，生成结果会立刻退化为乱码。这正是「形状一致不代表语义正确」的典型陷阱，也说明为什么最终要靠「能否生成连贯文本」来兜底验收。

**练习 2**：为什么映射前馈网络时只动了 `ff.layers[0]` 和 `ff.layers[2]`，没有 `layers[1]`？

**参考答案**：`ff.layers[1]` 是 `GELU` 激活函数，它没有任何可学习参数，自然没有权重需要搬运。

---

## 5. 综合实践：加载 gpt2-small 并生成连贯文本

把本讲三块内容串起来的验收任务：**下载 OpenAI 的 124M 权重 → 灌进我们手写的 `GPTModel` → 用 `Every effort moves you` 生成文本**。

**最省事的方式**是直接跑汇总脚本 `gpt_generate.py`，它把下载、映射、生成全串好了（[gpt_generate.py:230-251](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L230-L251)）：

```bash
cd ch05/01_main-chapter-code
python gpt_generate.py --prompt "Every effort moves you" --device cpu
```

`main()` 内部的流程是（[gpt_generate.py:232-251](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L232-L251)）：`download_and_load_gpt2` → `GPTModel(BASE_CONFIG)` → `load_weights_into_gpt` → `gpt.eval()` → `generate(..., top_k=50, temperature=1.0)`。

配置由 `BASE_CONFIG` 与 `model_configs` 拼出（[gpt_generate.py:281-293](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_generate.py#L281-L293)），其中两点是「为对齐 OpenAI 权重」而特意与 u4-l3 的 `GPT_CONFIG_124M` 不同的：`context_length=1024`（原始 GPT-2 用 1024，而非我们训练时的 256）、`qkv_bias=True`（要接收 OpenAI 的 QKV 偏置）。

**想看清每一步的方式**则在 notebook §5.5 里手写一遍，关键步骤是：

```python
from gpt_download import download_and_load_gpt2
settings, params = download_and_load_gpt2(model_size="124M", models_dir="gpt2")

NEW_CONFIG = GPT_CONFIG_124M.copy()
NEW_CONFIG.update({"emb_dim":768,"n_layers":12,"n_heads":12})
NEW_CONFIG.update({"context_length":1024, "qkv_bias":True})

gpt = GPTModel(NEW_CONFIG)
load_weights_into_gpt(gpt, params)
gpt.to(device); gpt.eval()

token_ids = generate(model=gpt,
    idx=text_to_token_ids("Every effort moves you", tokenizer).to(device),
    max_new_tokens=25, context_size=NEW_CONFIG["context_length"],
    top_k=50, temperature=1.5)
print(token_ids_to_text(token_ids, tokenizer))
```

**需要观察的现象与预期结果**：输出应是一段**语法通顺、语义连贯**的英文，例如（notebook 在 `temperature=1.5` 下得到）：

> *Every effort moves you toward finding an ideal new way to practice something! What makes us want to be on top of that?*

待本地验证：因温度/top-k 采样带随机性，你每次跑的具体文字会不同，但「连贯可读」这一点是稳定的。**对比验证**：若把 `load_weights_into_gpt(gpt, params)` 这一行注释掉（即用随机初始化的权重），同样的提示只会产出乱码——这一正一反的对比，就是「权重加载成功」最直观的证据。

> 进阶：把 `CHOOSE_MODEL` / `model_size` 换成 `355M`、`774M`、`1558M`（对应 `gpt_generate.py` 里 `model_configs` 的另外几档），即可加载更大的 GPT-2 变体，生成质量通常更好（代价是显存/内存与下载时间）。注意模型越大，`emb_dim`/`n_layers`/`n_heads` 都要跟着配置走。

## 6. 本讲小结

- **`state_dict` 是 PyTorch 的标准存档格式**：`torch.save(model.state_dict(), "model.pth")` 存盘，`new` 一个同结构模型后 `load_state_dict(torch.load(..., weights_only=True))` 还原；断点续训还要额外打包 `optimizer_state_dict`。
- **下载 OpenAI 权重要过 TensorFlow 这关**：`gpt_download.py` 下载 7 个 TF checkpoint 文件，并用 `tf.train.list_variables` / `load_variable` 把斜杠命名重组为嵌套字典 `params`；`gpt_download.py`（带备用源）比脚本内联版更健壮。
- **`load_weights_into_gpt` 是命名+布局的翻译官**：处理三类不对齐——OpenAI 命名 ↔ PyTorch 属性名、`Conv1D [in,out]` ↔ `Linear [out,in]`（靠 `.T`）、合并的 `c_attn` ↔ 拆分的 QKV（靠 `np.split`）。
- **`assign` 用「形状即契约」把住质量关**：形状不符立刻报错，但形状相同不代表语义正确（漏 `.T` 不会被拦），所以最终要靠「能否生成连贯文本」兜底验收。
- **加载时配置要对齐 OpenAI**：`context_length=1024`、`qkv_bias=True`，否则要么维度对不上、要么漏接偏置。
- **权重绑定在本讲落地**：`out_head` 复用 `wte`，填上了 u4-l3「163M 扣掉 out_head 才是 124M」的坑，也让编解码空间对称。

## 7. 下一步学习建议

- **横向**：本讲加载的是「TF 源」权重。若想绕开 TensorFlow，可读 `ch05/02_alternative_weight_loading/weight-loading-pytorch.ipynb`，它从 Hugging Face Hub 直接拉已转好的 PyTorch `.pth`，逻辑更简单，可作为对照。
- **纵向（第 6 章）**：有了能生成连贯文本的预训练 GPT，下一步就是**分类微调**——把 `out_head` 换成二分类头、冻结主干，在垃圾短信数据集上微调。届时你会再次用到本讲的「替换输出头」与「加载预训练权重」两件事。
- **工程深化（u10-l3）**：本讲的 `torch.load` 会把整个 state_dict 一次性读进内存。对于真正的大模型，这是不可接受的——`ch05/08_memory_efficient_weight_loading/` 讨论了内存高效的分片加载策略，学完第 6、7 章后值得回来读。
- **架构对比（u10-l1）**：本讲我们让自建模型与 GPT-2 逐参数对齐。若好奇 GPT 与 Llama 架构的差别（RoPE、RMSNorm 等），可提前翻 `ch05/07_gpt_to_llama/`，它会复用本讲建立的「加载预训练权重」工作流。

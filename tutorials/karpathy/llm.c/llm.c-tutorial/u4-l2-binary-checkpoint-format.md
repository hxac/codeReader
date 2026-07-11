# 权重 .bin 二进制协议与 checkpoint

## 1. 本讲目标

本讲聚焦 llm.c 的「跨语言契约」——`.bin` 权重文件格式。学完后你应当能够：

1. 画出 `gpt2_124M.bin` 的字节布局：1024 字节头（魔数 + 版本 + 6 个 config 整数 + 填充）后紧跟一整块张量流。
2. 说清 `write_model` 把 PyTorch 的 16 类参数按什么顺序写出，以及为什么是「按张量类型分组、跨层连续」而不是「逐 block 写」。
3. 区分 fp32 与 bf16 两种写出方式对应的版本号，以及 `pad_vocab` 把词表从 50257 填到 50304 的原因。
4. 看懂 C 端 `gpt2_build_from_checkpoint` 如何用一次 `fread` 把整块张量流「对称地」灌进预先排好指针的内存。

本讲是 u1-l4（数据 `.bin`）的姊妹篇：数据文件承载 *输入 token*，本讲的模型文件承载 *训练好的权重*。同时承接 u3-l4，那里消费的 `debug_state.bin` 也是用本讲的同一套头格式生成的。

## 2. 前置知识

在进入正文前，先回顾几个本讲会用到的概念（更细致的解释见前置讲义）：

- **token / 词表 (V)**：GPT-2 的真实词表大小 \(V = 50257\)。每个 token 是 `[0, V)` 内的一个整数。
- **张量扁平化 (flatten)**：一个 `(L, C)` 的二维数组在内存里就是 `L*C` 个连续 float，行主序排布。llm.c 全程用「一维数组 + 指针算术」表示多维张量（见 u1-l3、u2-l1）。
- **权重绑定 (weight tying)**：GPT-2 的输入查表 `wte` 与最终输出投影 `lm_head` 共享同一张 `(V, C)` 权重（见 u2-l1、u4-l1）。
- **`.bin` 头协议**：u1-l4 讲过数据 `.bin` 用「256 个 int32 = 1024 字节头」开头，前几格存魔数/版本/元信息。本讲的模型 `.bin` 复用同一套「固定 1024 字节头」思路，但**魔数不同**，以此区分文件种类。
- **size_t 防溢出**：参数总量约 1.24 亿，乘以 4 字节会逼近 `int` 上限，故 C 端统一用 `size_t`。

一个贯穿全讲的直觉：

> `.bin` 文件 = **自描述的内存快照**。它先写一份「说明书」（头：版本 + 形状），再写一整块「按 C 端布局排好序的原始字节」。C 端只要照着说明书算出每段大小、`malloc` 一整块、再一次性 `fread`，就能让所有指针自动就位。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `train_gpt2.py` | 写端：`write_model` / `write_state` / `write_tensors` / `write_fp32` / `write_bf16` / `pad_vocab` 把 PyTorch 权重序列化成 `.bin`。 |
| `train_gpt2.c` | 读端：`GPT2Config` / `ParameterTensors` / `fill_in_parameter_sizes` / `malloc_and_point_parameters` / `gpt2_build_from_checkpoint` 把 `.bin` 读回内存并指指针。 |
| `llmc/tokenizer.h` | 平行参照：另一份独立 `.bin`（tokenizer）的头格式，说明「魔数区分文件种类」是仓库通用约定。 |
| `llmc/utils.h` | 读端基础设施：`freadCheck` / `fopenCheck` / `mallocCheck` 等带错误检查的 IO 宏。 |

仓库里共有 **四种** `.bin` 文件，全部遵循「1024 字节头 + 载荷」的同一套思路，但靠**不同魔数**互相区分。本讲聚焦其中两种（模型与 state），另两种作对照：

| 文件 | 魔数 | 版本 | 载荷 | 产出函数 |
| --- | --- | --- | --- | --- |
| `gpt2_124M.bin`（权重） | 20240326 | 3 (fp32) / 5 (bf16) | 16 类参数张量流 | `write_model` |
| `gpt2_124M_debug_state.bin` | 20240327 | 2 | x, y, logits, loss, 16 类梯度 | `write_state` |
| `gpt2_tokenizer.bin` | 20240328 | 2 | token 字节表 | `write_tokenizer` |
| `*_train.bin`（数据，u1-l4） | 20240520 | 1 | uint16 token 流 | `write_datafile`（在 `dev/data/`） |

## 4. 核心概念与源码讲解

### 4.1 `.bin` 头格式与 `write_model`

#### 4.1.1 概念说明

PyTorch 的权重活在 `model.named_parameters()` 的字典里，名字带着模块层级（如 `transformer.h.0.attn.c_attn.weight`），并且是**逐 block 组织**的（`h.0` 的所有参数聚一起，再 `h.1`……）。但 C 端为了能用**一次 `fread`** 装满全部权重，要求所有同类参数跨层**连续存放**（比如 12 层的 `ln1w` 拼成一个 `(12, C)` 的大数组）。

因此 `.bin` 文件必须做两件事：

1. **自描述**：开一个固定 1024 字节的头，写明「这是哪种文件、什么版本、模型有多大」，让 C 端不看文件名就能解析。
2. **重排**：把 PyTorch 的逐 block 顺序，重排成 C 端期望的「按张量类型分组、跨层连续」顺序。

#### 4.1.2 核心流程

`write_model(model, filename, dtype)` 的执行流程：

```text
1. 由 dtype 选版本号：fp32 -> 3，bf16 -> 5
2. 新建 header = 256 个 int32（= 1024 字节），全 0
3. header[0] = 20240326 (magic)
   header[1] = version
   header[2..6] = block_size, vocab_size, n_layer, n_head, n_embd
4. 取 wte (V, C) -> pad_vocab -> (Vp, C)，header[7] = Vp
5. 打开文件，先写 header（1024 字节），再调 write_tensors 写参数流
```

头的 8 个有用槽位如下（其余 248 个槽位保持 0，纯属「占位填充到 1024 字节」，留作日后扩展）：

| 槽位 | 含义 | GPT-2 124M 取值 |
| --- | --- | --- |
| `[0]` | magic | 20240326 |
| `[1]` | 版本（3=fp32，5=bf16） | 3 或 5 |
| `[2]` | `max_seq_len` / block_size | 1024 |
| `[3]` | `vocab_size`（真实 V） | 50257 |
| `[4]` | `num_layers`（L） | 12 |
| `[5]` | `num_heads`（NH） | 12 |
| `[6]` | `channels` / n_embd（C） | 768 |
| `[7]` | `padded_vocab_size`（Vp） | 50304 |

注意 `vocab_size` 与 `padded_vocab_size` 是两个不同字段，前者是真实词表 50257，后者是填充后的 50304——这个区别贯穿本讲与 u2-l6。

#### 4.1.3 源码精读

先看头的写入：[train_gpt2.py:449-477](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L449-L477)。关键几句：

- `header = torch.zeros(256, dtype=torch.int32)` 建一个 1024 字节的全零缓冲，注释明确写了「version int, GPTConfig ints, padding to 1024 bytes」。
- `header[0] = 20240326` 写魔数；`header[2..6]` 直接抄 `model.config` 的 5 个字段；`header[7] = wte_padded.size(0)` 写填充后的词表大小。
- `file.write(header.numpy().tobytes())` 先落头，再 `write_tensors(...)` 落参数。

版本号到 magic 的映射在函数开头：[train_gpt2.py:452-456](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L452-L456)——`"float32": 3`、`"bfloat16": 5`。

接下来看 `write_tensors` 如何把 16 类参数按 C 端顺序写出：[train_gpt2.py:395-426](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L395-L426)。读它时注意**循环结构**：每个 `for i in range(L)` 循环只写**某一类**参数，跨所有层。这正是「按类型分组、跨层连续」。

把这 16 类参数与 C 端的 `ParameterTensors`（[train_gpt2.c:536-554](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L536-L554)）逐位对照如下（顺序必须**完全一致**，否则指针错位、整个模型废掉）：

| # | C 端指针 | 形状 | PyTorch 来源 | write_tensors 中的循环 |
| --- | --- | --- | --- | --- |
| 0 | `wte` | (Vp, C) | `transformer.wte.weight` | 单次（已 pad） |
| 1 | `wpe` | (maxT, C) | `transformer.wpe.weight` | 单次 |
| 2 | `ln1w` | (L, C) | `h.{i}.ln_1.weight` | `for i in range(L)` |
| 3 | `ln1b` | (L, C) | `h.{i}.ln_1.bias` | 另一个 `for i in range(L)` |
| 4 | `qkvw` | (L, 3C, C) | `h.{i}.attn.c_attn.weight` | 循环 |
| 5 | `qkvb` | (L, 3C) | `h.{i}.attn.c_attn.bias` | 循环 |
| 6 | `attprojw` | (L, C, C) | `h.{i}.attn.c_proj.weight` | 循环 |
| 7 | `attprojb` | (L, C) | `h.{i}.attn.c_proj.bias` | 循环 |
| 8 | `ln2w` | (L, C) | `h.{i}.ln_2.weight` | 循环 |
| 9 | `ln2b` | (L, C) | `h.{i}.ln_2.bias` | 循环 |
| 10 | `fcw` | (L, 4C, C) | `h.{i}.mlp.c_fc.weight` | 循环 |
| 11 | `fcb` | (L, 4C) | `h.{i}.mlp.c_fc.bias` | 循环 |
| 12 | `fcprojw` | (L, C, 4C) | `h.{i}.mlp.c_proj.weight` | 循环 |
| 13 | `fcprojb` | (L, C) | `h.{i}.mlp.c_proj.bias` | 循环 |
| 14 | `lnfw` | (C,) | `transformer.ln_f.weight` | 单次 |
| 15 | `lnfb` | (C,) | `transformer.ln_f.bias` | 单次 |

`NUM_PARAMETER_TENSORS` 宏固定为 16：[train_gpt2.c:536-536](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L536-L536)。

**一个易被忽略但极关键的细节**：PyTorch 的 `lm_head` 与 `wte` 权重绑定（同一个张量，见 u4-l1）。导出时只有 `transformer.wte.weight` 这一份（`lm_head` 不在 `named_parameters()` 里单独出现），所以表里第 0 行既是输入查表也是输出投影。这解释了为什么表里**只有 16 类**而不是 17 类。

#### 4.1.4 代码实践

**目标**：用最小的 Python 脚本只读 `.bin` 的头，验证魔数与 config，无需加载整个权重。

**操作步骤**：

1. 先按本讲末尾「综合实践」的方式产出 `gpt2_124M.bin`（或复用 `dev/download_starter_pack.sh` 下到的同名文件）。
2. 运行下面这段**示例代码**（不是项目原有代码，仅用于查看头）：

```python
# 示例代码：只读 .bin 模型文件的头
import numpy as np
with open("gpt2_124M.bin", "rb") as f:
    header = np.frombuffer(f.read(256 * 4), dtype=np.int32)
print("magic   :", header[0], "(期望 20240326)")
print("version :", header[1], "(3=fp32, 5=bf16)")
print("block_size / V / L / NH / C :", header[2:7])
print("padded_vocab_size Vp        :", header[7])
```

**需要观察的现象**：`magic` 应为 `20240326`，`version` 为 `3`（starter pack 是 fp32 版），`V=50257`、`Vp=50304`、`L=12`、`NH=12`、`C=768`。

**预期结果**：头的 8 个有效槽位与上表完全一致；剩余槽位为 0。若 `version` 不是 3 而是 5，说明这是 bf16 版（`gpt2_124M_bf16.bin`），CPU 版 `train_gpt2.c` 会拒绝它（见 4.3）。

**待本地验证**：starter pack 里的文件是否确为 version 3，请运行上述脚本确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么头要固定填到 1024 字节，而不是只写 8 个 int（32 字节）就结束？

**答案**：留出扩展空间。日后若要新增 config 字段（如新的模型变体描述符），可在已有头里追加而不破坏旧文件的解析——C 端只要 `fread` 固定 256 个 int，多出来的槽位暂时是 0，互不影响。同时 1024 字节是磁盘对齐友好值。

**练习 2**：若有人误把 `gpt2_tokenizer.bin`（魔数 20240328）当作权重喂给 `gpt2_build_from_checkpoint`，会在哪一行失败？

**答案**：在 [train_gpt2.c:713-713](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L713-L713) 的 `if (model_header[0] != 20240326)` 处失败，打印 `Bad magic model file` 并 `exit(1)`。魔数正是用来把不同种 `.bin` 挡在各自的读取入口外。

### 4.2 fp32/bf16 写出与 `pad_vocab`

#### 4.2.1 概念说明

权重可以用两种精度写出：

- **fp32**：每个元素 4 字节，全精度，C 端 CPU 参考实现就读这种（version 3）。
- **bf16**：每个元素 2 字节，省一半磁盘与显存，CUDA 主线混合精度训练加载这种（version 5）。

`pad_vocab` 解决另一个工程问题：真实词表 50257 是个质数级别、对 GPU 极不友好的尺寸。很多矩阵运算（cuBLAS 分块、张量核对齐）喜欢列数是 8/16/32/128 的倍数。于是导出时把 `wte` 在词表维度上**补零**到最近的 128 的倍数 50304。这个补零**算法上是 no-op**（填充行权重为 0，且 softmax/交叉熵只在真实 V 上算，见 u2-l6），纯粹为换运行效率。

#### 4.2.2 核心流程

两种写出的差异只在「如何把一个 torch 张量变成字节」：

```text
write_fp32(t): detach -> cpu -> float32 -> numpy().tobytes()      # 4 字节/元素
write_bf16(t): detach -> cpu -> bfloat16 -> view(int16) -> tobytes()  # 2 字节/元素
```

bf16 那条路要绕个弯：**numpy 没有 bf16 类型**，所以先把 bf16 张量 `view(torch.int16)` 重新解释成 16 位整数，再 `.tobytes()`——字节布局完全一致，只是骗过 numpy 的类型系统。注意 C 端必须知道这是 bf16，用 2 字节步长去读（主线 `.cu` 里由 `floatX` 别名处理，见 u5）。

`pad_vocab` 的填充计算：

\[
V_p = \left\lfloor \frac{V + m - 1}{m} \right\rfloor \cdot m,\qquad m = 128,\ V = 50257
\]

代入得 \(V_p = \lfloor (50257+127)/128 \rfloor \cdot 128 = 393 \times 128 = 50304\)，需补 \(50304 - 50257 = 47\) 行零。

#### 4.2.3 源码精读

写出工具：[train_gpt2.py:383-393](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L383-L393)。`write_bf16` 里那行 `t = t.view(torch.int16)` 就是上述「重新解释位模式」的技巧。

`write_tensors` 顶部的选择器：[train_gpt2.py:397-398](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L397-L398)——`write_fun = write_fp32 if dtype == "float32" else write_bf16`。整套参数流用同一个 `write_fun`，保证精度一致。

`pad_vocab` 的定义：[train_gpt2.py:428-447](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L428-L447)。注意几个防御点：`assert tensor.ndim == 2`、`assert V == 50257`（写死真实词表）、`value=0`（填零）。函数里 `Vp = ((V + multiple - 1) // multiple) * multiple` 正是上面的整数上取整。

填充在导出边界发生：[train_gpt2.py:466-472](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L466-L472)。`write_model` 取出 `wte`、调 `pad_vocab` 得到 `(Vp, C)`，**替换回 `params` 字典**，再把 `Vp` 写进 `header[7]`。这就是为什么 PyTorch 侧的 `GPTConfig` **没有** `padded_vocab_size` 字段（见 u4-l1）——填充只活在「导出到 C」这一刻。

**重要补充：`write_state` 始终用 fp32**。[train_gpt2.py:506-506](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L506-L506) 末尾那行 `write_tensors(grads, ..., "float32")` 写死 fp32，注释说「always store these in fp32 to have an accurate reference」。debug state 是正确性标尺（u3-l4），必须高精度；而它对 `wte` 的梯度同样做 `pad_vocab`（[train_gpt2.py:490-492](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L490-L492)），与 `write_model` 镜像。

一次运行同时产出 fp32 与 bf16 两份权重：[train_gpt2.py:694-695](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L694-L695)。于是同一个 `gpt2_124M.bin`（v3, fp32）给 CPU 参考，`gpt2_124M_bf16.bin`（v5, bf16）给 CUDA 主线。

#### 4.2.4 代码实践

**目标**：手算 `pad_vocab`，并用文件大小交叉验证精度。

**操作步骤**：

1. 在纸上算 \(V_p\)（应得 50304，补 47 行）。
2. （可选）若已生成 `.bin`，用 `ls -l` 或 `stat` 看文件大小，再用下式反推精度。
   - 16 类参数总元素数 ≈ 124M（精确数见 4.3 的 `num_parameters`）。
   - 文件大小 ≈ \(1024\,\text{字节头} + \text{num\_parameters} \times 4\)（fp32）或 \(\times 2\)（bf16）。

**需要观察的现象**：fp32 版的 `.bin` 大小约为 bf16 版的 **两倍**（头都是 1024 字节，差异只在载荷）。

**预期结果**：fp32 版约 0.5 GB 量级，bf16 版约一半。两份文件的前 8 字节（magic + version）相同部分里 magic 都是 20240326，但 version 一份是 3、一份是 5。

**待本地验证**：具体字节数请以本地生成的文件实际大小为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `write_bf16` 要先 `view(torch.int16)` 再 `tobytes()`？

**答案**：因为 numpy 没有原生的 bf16 数据类型，直接 `.numpy()` 会报错。bf16 与 int16 都是 16 位，位模式逐位相同，`view(torch.int16)` 只是让 numpy 用它认识的 16 位整数类型去解释那段内存，`.tobytes()` 落盘的字节与 bf16 完全一致。

**练习 2**：如果把 `pad_vocab` 的 `multiple` 从 128 改成 32，`Vp` 会变成多少？会破坏算法正确性吗？

**答案**：\(V_p = \lceil 50257/32 \rceil \times 32 = 1571 \times 32 = 50272\)。不会破坏算法正确性——填充行权重为 0 且不参与 softmax/交叉熵（只在真实 V=50257 上算）。但会破坏**二进制兼容**：C 端从 `header[7]` 读到 50272，而 u2 各层算子假设的 `Vp` 也得跟着变；只要头与权重流一致即可，但已生成的旧 `.bin` 不再匹配。

### 4.3 C 端对称读取：`gpt2_build_from_checkpoint`

#### 4.3.1 概念说明

写端把 16 类参数按「C 端布局」排成一条连续字节流；读端只要**对称地**做三件事就能复原：

1. 读 1024 字节头，校验魔数与版本，解析出 6 个 config + `Vp`。
2. 用 config 算出 16 类参数各自的大小，一次 `malloc` 一整块，再把 16 个指针「钉」进各自的偏移。
3. **一次 `fread`** 把整块张量流原样灌进这块内存。

关键直觉：因为写端按 C 期望的顺序连续写，读端的指针排布顺序与写端**逐位对应**，所以一次大 `fread` 就能让所有指针自动就位，无需逐张量循环读取。

#### 4.3.2 核心流程

`gpt2_build_from_checkpoint(model, path)` 的流程：

```text
1. fopenCheck(path) 打开文件
2. freadCheck(header, int, 256)   读 1024 字节头
3. 校验 header[0]==20240326, header[1]==3   (注意：只认 fp32 的版本 3!)
4. 把 header[2..7] 翻译成 config 的 6 个字段 + Vp
5. fill_in_parameter_sizes()  用 config 算 16 类参数大小
6. 累加得到 num_parameters
7. malloc_and_point_parameters()  一次 malloc + 钉 16 个指针
8. freadCheck(params_memory, float, num_parameters)  一次灌满
9. fcloseCheck；把各项运行态(acts/grads/m_memory 等)置 NULL/0（懒分配）
```

第 3 步的版本校验只接受 `3`——这是本模块最重要的约束：**CPU 参考实现 `train_gpt2.c` 只读 fp32 版（version 3），不读 bf16 版（version 5）**。bf16 由 CUDA 主线 `.cu`（u5）用 `floatX` 处理。

#### 4.3.3 源码精读

读入口：[train_gpt2.c:707-763](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L707-L763)。逐段看：

- 头校验：[train_gpt2.c:711-718](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L711-L718)。`model_header[0] != 20240326` 报 `Bad magic`；`model_header[1] != 3` 报 `Bad version` 并提示「重跑 `python train_gpt2.py`」。
- config 解析：[train_gpt2.c:721-727](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L721-L727)。注意变量全用 `size_t`（注释 `// size_t to prevent int overflow`），且顺序与写端 `header[2..7]` **一一对应**：maxT, V, L, NH, C, Vp。
- 大小计算：`fill_in_parameter_sizes` [train_gpt2.c:556-577](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L556-L557)。这里第 0 项 `param_sizes[0] = Vp * C`——用的是**填充后**的 `Vp`，所以读进来的 `wte` 就是 `(50304, 768)`，与写端 `pad_vocab` 后的形状吻合。
- 指针排布：`malloc_and_point_parameters` [train_gpt2.c:580-599](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L580-L599)。这是 u1-l3 讲过的「一次性 malloc + 指针排布」技巧：把 `&params->wte` 等 16 个「指向指针的指针」放进数组 `ptrs[]`，让 `params_memory_iterator` 依次累加偏移，逐个钉到位。`ptrs[]` 的顺序与 4.1 表里 16 类参数**完全一致**——这是「一次 fread 自动就位」的根本保证。
- 一次灌满：[train_gpt2.c:748-750](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L748-L750)。`malloc_and_point_parameters` 返回 `params_memory`，`freadCheck(params_memory, sizeof(float), num_parameters, model_file)` 一次性把整块参数流读进来，然后 `fcloseCheck`。
- 懒分配收尾：[train_gpt2.c:752-762](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L752-L762)。`acts_memory`、`grads_memory`、`m_memory`、`v_memory` 等都置 `NULL`，`mean_loss = -1.0f`——它们依赖运行时的 B、T，推迟到首次前向/反向/更新时才分配（见 u1-l3）。

读取所用的 IO 宏来自 [llmc/utils.h:44-64](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/utils.h#L44-L64)：`freadCheck` 包装 `fread` 并在「读到元素数 ≠ 预期」时打印诊断信息后 `exit`。这正是「文件被截断 / 版本不对」时给出友好报错的来源。

**对称性总结**：写端 `write_model`（头 + 重排张量流）↔ 读端 `gpt2_build_from_checkpoint`（读头 + 算大小 + 钉指针 + 一次 fread）。两端的 16 类顺序、字段含义、`Vp` 处理完全镜像，魔数 + 版本号是它们互相辨认的暗号。

#### 4.3.4 代码实践

**目标**：用源码阅读法验证「读端顺序与写端逐位对应」，无需运行。

**操作步骤**：

1. 打开 [train_gpt2.c:588-592](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L588-L592) 的 `ptrs[]` 数组，抄下 16 个指针的顺序：`wte, wpe, ln1w, ln1b, qkvw, qkvb, attprojw, attprojb, ln2w, ln2b, fcw, fcb, fcprojw, fcprojb, lnfw, lnfb`。
2. 对照 4.1.3 的表，确认与 `write_tensors` 的写出顺序**逐行一致**。
3. 再看 [train_gpt2.c:561-576](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L561-L576) 的 `param_sizes[0..15]`，确认第 0 项用 `Vp`（填充后），其余与写端形状一致。

**需要观察的现象**：三个地方（`ptrs[]`、`param_sizes[]`、`write_tensors` 的写出循环）描述的是**同一个顺序**，只是三种表达。

**预期结果**：若任意一处顺序错位，模型权重会被错误地解释（例如把 `wpe` 当成 `wte` 的一部分），前向 loss 会立刻异常。这就是为什么这三处必须严格一致——也是「魔数 + 版本」之外，格式正确性的第二道保险。

#### 4.3.5 小练习与答案

**练习 1**：为什么读端能「一次 `fread`」读全部参数，而不必逐张量循环？

**答案**：因为写端 `write_tensors` 已经按 C 端期望的顺序把 16 类参数连续排好，且 C 端 `malloc_and_point_parameters` 用同序的 `ptrs[]` 把指针钉进同一块连续内存。两端顺序逐位对应，所以这一大块字节可以原样灌进 `params_memory`，16 个指针自动各自指向正确的起始偏移。

**练习 2**：把 `gpt2_124M_bf16.bin`（version 5）喂给 `train_gpt2.c` 的 `gpt2_build_from_checkpoint` 会怎样？

**答案**：在 [train_gpt2.c:714-718](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L714-L718) 处因 `model_header[1] != 3` 失败，打印 `Bad version in model file` 并提示重跑 `python train_gpt2.py`，然后 `exit(1)`。即便强行跳过版本检查，由于 `freadCheck` 按 `sizeof(float)`（4 字节）读，而 bf16 是 2 字节，会把两个 bf16 拼成一个错误的 float，模型数值全错。bf16 版只能由 CUDA 主线（u5）用 `floatX` 别名正确加载。

## 5. 综合实践

**任务**：亲手跑一遍「PyTorch 写 → 查看 → C 读」的完整闭环，把 `.bin` 格式从头到尾串起来。

**步骤**：

1. **生成文件**。在仓库根目录运行（需要 PyTorch + 能联网下载 GPT-2 权重）：

   ```bash
   # 默认 model=gpt2 即 124M，会写 gpt2_124M.bin / _bf16.bin / _debug_state.bin / gpt2_tokenizer.bin
   python train_gpt2.py --num_iterations=0
   ```

   若无 GPU/网络，可改用 `dev/download_starter_pack.sh` 下到现成的 `gpt2_124M.bin` 与 `gpt2_124M_debug_state.bin`（但拿不到 bf16 版与自产 tokenizer，可用 u1-l2 的 CPU 流程补上）。

2. **查头**。运行 4.1.4 的示例脚本，确认 magic=20240326、version=3、`Vp=50304`。

3. **对照 16 类写出顺序**。打开 [train_gpt2.py:399-426](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L399-L426)，逐条把每个 `write_fun(...)` 标到 4.1.3 的表上，验证「按类型分组、跨层连续」。

4. **回答两个关键问题**（本讲规格要求）：
   - **16 个参数张量的写出顺序是什么？** —— 按 `wte, wpe` 单写，再对每类（ln1w→ln1b→qkvw→qkvb→attprojw→attprojb→ln2w→ln2b→fcw→fcb→fcprojw→fcprojb）各跑一个跨所有层的循环，最后单写 `lnfw, lnfb`。
   - **为什么 `padded_vocab_size=50304` 而非 50257？** —— 真实词表 50257 对 GPU 矩阵运算不友好（不是 2 的幂、不整除常见 tile 尺寸），`pad_vocab` 把它上取整到最近的 128 倍数得 50304，补 47 行零。由于填充行权重为 0 且 softmax/交叉熵只在真实 V 上算，它是**算法 no-op**，只换来更高的访存/张量核效率。

5. **C 端闭环**（可选）。编译运行 CPU 版，看它读你（或 starter pack）的 `.bin`：

   ```bash
   make train_gpt2
   OMP_NUM_THREADS=4 ./train_gpt2
   ```

   开头会打印 `[GPT-2]` 与 `max_seq_len / vocab_size / padded_vocab_size / num_layers / num_heads / channels / num_parameters`，确认这些值与头完全一致；随后 loss 从约 5.3 开始下降，即说明「写端 → 头 → 读端」整条契约打通。

**预期结果**：第 2 步的头字段、第 5 步 C 端打印的 config，与本讲给出的 124M 数值（1024/50257/50304/12/12/768）逐一吻合。若 num_parameters 约 1.24 亿，说明 `Vp*C`（wte 一项就占约 3863 万）等大小计算正确。

**待本地验证**：实际 loss 曲线、文件字节数请以本地运行为准；本任务不假装已替你跑过命令。

## 6. 本讲小结

- `.bin` 模型文件 = **1024 字节自描述头**（魔数 20240326 + 版本 + 6 个 config + `Vp`，其余填零）+ 一整块**按 C 端布局排好序的张量流**。
- `write_model` 把 PyTorch 的 16 类参数**按类型分组、跨层连续**重排写出，顺序与 C 端 `ParameterTensors` 的 `ptrs[]`、`param_sizes[]` 三处逐位对应——这是「一次 fread 自动就位」的根本保证。
- fp32 → version 3（4 字节/元素，CPU 参考读这种），bf16 → version 5（2 字节/元素，CUDA 主线读这种，靠 `view(int16)` 绕过 numpy 无 bf16 的限制）；`write_state` 永远用 fp32 以做精确标尺。
- `pad_vocab` 把词表 50257 上取整到 128 的倍数 50304（补 47 行零），算法上 no-op，只为 GPU 矩阵运算对齐；填充发生在导出边界，所以 PyTorch 侧 `GPTConfig` 无此字段。
- 读端 `gpt2_build_from_checkpoint` 是写端的镜像：读头校验（只认 version 3）→ `fill_in_parameter_sizes` → `malloc_and_point_parameters` 钉指针 → **一次 `fread`** 灌满；激活/梯度/优化器状态留作懒分配。
- 仓库用**不同魔数**区分四类 `.bin`（权重 20240326 / state 20240327 / tokenizer 20240328 / 数据 20240520），同一套「1024 字节头」思路贯穿全局。

## 7. 下一步学习建议

- 想看 `.bin` 的 debug state 在 C 端如何被逐元素比对，直接进 **u3-l4**（`test_gpt2.c`），那里消费本讲 `write_state` 产出的 `gpt2_124M_debug_state.bin`。
- 想了解 bf16 版（version 5）如何被正确加载、`floatX` 精度宏如何工作，进入 **u5-l1**（CUDA 主线架构与 llmc 头文件库）。
- 若对权重绑定（`wte` 兼任 `lm_head`）在导出与反向中的细节感兴趣，回看 **u4-l1**（PyTorch 参考）与本讲的 4.1.3 注解。
- 后续 **u6-l1** 会讲解训练时为何仍需 fp32 master weights，与本讲「bf16 落盘 + fp32 state」的精度策略一脉相承，可对照阅读。

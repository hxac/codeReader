# 数值正确性测试：对照 PyTorch debug state

## 1. 本讲目标

llm.c 的卖点之一是「用一份朴素的 C 代码精确复现 PyTorch 的 GPT-2 训练」。但「精确」不能只靠嘴说——必须有一个客观判据。本讲讲解的 `test_gpt2.c` 就是这个判据：它把 PyTorch 跑出来的「标准答案」序列化成一个 `.bin` 文件，再让 C 实现逐位去比对。

学完本讲，你应当能够：

- 说清楚 `gpt2_124M_debug_state.bin` 里存了什么、由谁生成、为什么必须用 fp32。
- 读懂 `test_gpt2.c` 的三道比对关卡：第 0 步的 logits / loss / 梯度逐元素比对，以及 10 步训练的 loss 曲线回归。
- 解释容差（logits/loss 用 `1e-2`、梯度用 `2e-2`）与失败判定逻辑，并理解为什么「10 步 loss 曲线」比单点比对更能抓 bug。
- 独立编译运行 `make test_gpt2 && ./test_gpt2`，确认最后一行打印 `overall okay: 1`。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（它们在前置讲义中已建立）：

- **训练四步**：前向 `gpt2_forward` → 清零梯度 `gpt2_zero_grad` → 反向 `gpt2_backward` → 更新 `gpt2_update`（见 u1-l3、u3-l1、u3-l2）。
- **`+=` 累加 + 每步清零**约定：所有层反向都用 `+=` 累加梯度，因此每步开头必须 `gpt2_zero_grad` 把权重梯度和激活梯度清零（见 u2 系列、u3-l1）。
- **V 与 Vp 的区别**：真实词表 `V = 50257`，对齐填充后的 `padded_vocab_size Vp = 50304`（128 对齐），二者贯穿 logits/probs 的内存布局（见 u2-l6、u2-l7）。
- **一次性 malloc + 指针排布**：参数张量被一次性 `malloc` 成一整块，再用 `malloc_and_point_parameters` 把 16 个指针「钉」进不同偏移（见 u1-l3）。
- **AdamW 解耦权重衰减**：`θ -= lr·(m̂/(√v̂+ε) + wd·θ)`，衰减项作为独立加法项（见 u3-l2）。

本讲用到的新术语：

- **debug state**：PyTorch 在「初始权重上做一次前向 + 一次反向」后导出的标准答案二进制。
- **回归测试（regression test）**：把一段已知正确的输出曲线作为「指纹」，每次改代码后重跑，看指纹是否还在。
- **容差（tolerance）**：浮点逐位相等不现实，允许的最大绝对差。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `test_gpt2.c` | 本讲主角。`#define TESTING` 后 `#include "train_gpt2.c"`，复用全部模型代码，只新写一个 `main` 做比对。 |
| `train_gpt2.c` | 被测对象。提供 `GPT2` 结构体、`gpt2_build_from_checkpoint/forward/zero_grad/backward/update` 等函数，以及参数张量顺序定义。 |
| `train_gpt2.py` | 标准答案的**生产者**。其 `write_state` 函数生成 `gpt2_124M_debug_state.bin`。 |
| `llmc/utils.h` | 提供 `freadCheck` / `fopenCheck` / `mallocCheck` 等带错误检查的 I/O 宏。 |
| `Makefile` | 提供 `test_gpt2` 构建目标。 |

## 4. 核心概念与源码讲解

### 4.1 测试如何复用 train_gpt2.c：TESTING 宏与逐张量检查器

#### 4.1.1 概念说明

写测试最怕「被测代码」和「测试里粘的另一份代码」慢慢分叉。llm.c 用一个极简技巧避免这一点：`test_gpt2.c` 不复制任何模型代码，而是直接把 `train_gpt2.c` 整个 `#include` 进来，只在编译期用宏切换掉 `main` 函数。这样测试永远测的是「真·当前源码」。

#### 4.1.2 核心流程

1. `test_gpt2.c` 顶部先 `#define TESTING`，再 `#include "train_gpt2.c"`。
2. `train_gpt2.c` 用 `#ifndef TESTING … #endif` 把自己那个 demo 性质的 `main`（训练 40 步打印 loss 的脚本）包起来。
3. 于是当编译 `test_gpt2.c` 时，`train_gpt2.c` 的全部函数（各层前向/反向、`gpt2_forward/backward/update` 等）都被纳入，唯独它的 `main` 被跳过；`test_gpt2.c` 自己提供一个专门做比对的 `main`。

```c
#define TESTING
#include "train_gpt2.c"
```

参见 [test_gpt2.c:1-2](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L1-L2)：这两行就是整个测试复用机制的基石。

参见 [train_gpt2.c:1046-1047](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1046-L1047)：`#ifndef TESTING` 守卫让训练版的 `main`（含 random sampler、dataloader、40 步循环）在测试编译时被排除。

> 构建侧：`Makefile` 的 `test_gpt2` 目标用与 `train_gpt2` 完全相同的编译规则处理 `test_gpt2.c`，输出可执行文件名 `test_gpt2`（`OUTPUT_FILE = -o $@`，目标名即文件名）。参见 [Makefile:267-268](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L267-L268)。

#### 4.1.3 源码精读：check_tensor 逐张量检查器

测试需要一个反复使用的「逐元素比对两个浮点数组」的工具函数 `check_tensor`：

```c
int check_tensor(float *a, float *b, int n, const char* label) {
    int ok = 1;
    float maxdiff = 0.0f;
    float tol = 2e-2f;
    for (int i = 0; i < n; i++) {
        float diff = fabsf(a[i] - b[i]);
        ok = ok && (diff <= tol);
        if (diff > maxdiff) { maxdiff = diff; }
        // 前 5 个元素逐对打印，便于肉眼核对
    }
    // ok 则打印 TENSOR OK 并附带 maxdiff，否则 TENSOR NOT OK
    return ok;
}
```

参见 [test_gpt2.c:5-37](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L5-L37)。

要点：

- **容差是 `2e-2f`（0.02）**——比 logits/loss 比对用的 `1e-2` 更宽松，因为梯度经多层累加、数值误差更大。
- 它不仅返回 `ok` 布尔，还打印 `maxdiff = %e`，让你能看到「错多远」而不只是「错没错」。前 5 个元素成对打印（`printf("%f %f\n", a[i], b[i])`）用于肉眼定性核对。

> 这个「打印 maxdiff + 前几对数值」的设计非常实用：测试失败时你能立刻判断是「差一点点（数值噪声）」还是「差几个数量级（算法错了）」。

#### 4.1.4 代码实践

1. **目标**：理解 `#include` 复用机制如何避免代码分叉。
2. **步骤**：打开 `train_gpt2.c` 找到 `#ifndef TESTING`（约 1046 行），往下翻能看到被它包住的训练版 `main`（含 `random_f32`、dataloader、40 步循环、采样生成）。再对比 `test_gpt2.c` 顶部的两行。
3. **观察**：你会看到训练版 `main` 里调用的 `gpt2_forward/gpt2_zero_grad/gpt2_backward/gpt2_update`，与测试版 `main` 调用的是**同一个**函数实现——没有任何复制粘贴。
4. **预期结果**：从工程上确认，这个测试永远在测当前 `train_gpt2.c` 的真实行为。

### 4.2 模块一：debug state 的加载与来源

#### 4.2.1 概念说明

`gpt2_124M_debug_state.bin` 是 PyTorch 参考实现导出的「标准答案包」。它记录的是：**在初始权重（即 `gpt2_124M.bin` 这个 checkpoint）上，对固定的一批数据 (x, y) 做一次前向 + 一次反向**，所得到的全部中间标准结果。C 实现做完同样的前向 + 反向后，逐项与之比对。

为什么必须用 fp32 存？因为这是一把「尺子」，尺子本身的精度误差会污染所有判断。bf16 只有 8 位尾数，相对误差可达 \(2^{-7}\approx 0.008\)，与 `1e-2` 容差同量级，会模糊真正的算法 bug。所以 debug state 一律 fp32。

#### 4.2.2 核心流程：debug state 的文件布局

debug state 的二进制布局（由 PyTorch 的 `write_state` 写出，C 的 `test_gpt2.c` 对称读入）：

```
[ 1024 字节头 = 256 个 int32 ]
  header[0] = 20240327   // 魔数（区别于权重文件的 20240326、tokenizer 的 20240328）
  header[1] = 2          // state 版本（version 2：含 padded vocab 改动）
  header[2] = B          // 这批数据的 batch size（如 4）
  header[3] = T          // 序列长度（如 64）
[ 数据流，全部 fp32 或 int32 ]
  x        : B*T 个 int32   // 输入 token id
  y        : B*T 个 int32   // 目标 token id
  logits   : B*T*V 个 fp32  // 注意是 V（50257，未填充），不是 Vp
  loss     : 1 个 fp32       // 标量交叉熵损失
  grads    : num_parameters 个 fp32  // 全部 16 个参数张量的梯度，顺序与权重一致
```

#### 4.2.3 源码精读

先看 PyTorch 这一侧怎么生产它。`write_state` 的核心：

```python
def write_state(model, x, y, logits, loss, filename):
    header = torch.zeros(256, dtype=torch.int32)
    header[0] = 20240327 # magic
    header[1] = 2        # run state version = 2
    header[2] = x.size(0) # B
    header[3] = x.size(1) # T
    grads = {name: param.grad.cpu() for name, param in model.named_parameters()}
    wte_grad_padded = pad_vocab(wte_grad, value=0)   # wte 梯度也补齐到 Vp，补 0
    ...
    file.write(x.cpu().numpy().astype("int32").tobytes())  # x
    file.write(y.cpu().numpy().astype("int32").tobytes())  # y
    write_fp32(logits.cpu(), file)                          # logits (B,T,V)
    write_fp32(loss.cpu(), file)                            # loss
    write_tensors(grads, model.config.n_layer, file, "float32")  # 梯度，fp32
```

参见 [train_gpt2.py:479-507](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L479-L507)。

**关键前提**：debug state 是在**优化器介入之前**产生的。看生产侧的调用点：

```python
# do one forward pass to generate ground truth for our C tests
if master_process and args.write_tensors and (not args.inference_only):
    x, y = train_loader.next_batch()
    logits, loss = model(x, y)
    loss.backward()
    ...
    write_model(model, f"gpt2_124M.bin", dtype="float32")           # 初始权重
    write_state(model, x, y, logits, loss, f"gpt2_124M_debug_state.bin")  # 标准答案
```

参见 [train_gpt2.py:685-698](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L685-L698)。

注意此时只做了 `model(x,y)` 和 `loss.backward()`，**还没有 `optimizer.step()`**。这意味着：

- debug state 里的 logits / loss / grads **只依赖前向 + 反向**，与学习率、权重衰减、betas 等**优化器超参无关**。
- 梯度的张量顺序由 `write_tensors` 决定：wte, wpe, ln1w, ln1b, qkvw, qkvb, attprojw, attprojb, ln2w, ln2b, fcw, fcb, fcprojw, fcprojb, lnfw, lnfb。参见 [train_gpt2.py:395-426](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L395-L426)。这与 C 端 `fill_in_parameter_sizes` / `malloc_and_point_parameters` 的指针顺序**完全一致**（见 [train_gpt2.c:556-576](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L556-L576) 与 [train_gpt2.c:580-599](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L580-L599)），所以 C 端按相同顺序 `fread` 即可对齐。

再看 C 这一侧的对称读取：

```c
FILE *state_file = fopen("gpt2_124M_debug_state.bin", "rb");
int state_header[256];
freadCheck(state_header, sizeof(int), 256, state_file);
if (state_header[0] != 20240327) { printf("Bad magic state file\n"); return 1; }
if (state_header[1] != 2) { /* 提示重新跑 python train_gpt2.py */ return 1; }
int B = state_header[2];
int T = state_header[3];
...
freadCheck(x, sizeof(int), B*T, state_file);
freadCheck(y, sizeof(int), B*T, state_file);
freadCheck(expected_logits, sizeof(float), B*T*V, state_file);   // 只读 V 个！
freadCheck(expected_loss, sizeof(float), 1, state_file);
freadCheck(expected_grads_memory, sizeof(float), model.num_parameters, state_file);
```

参见 [test_gpt2.c:52-83](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L52-L83)。

注意三个对齐细节：

1. **魔数校验**：`20240327`，与权重文件的 `20240326`、tokenizer 的 `20240328` 都不同（回顾 u1-l4 的格式契约）。版本不对会提示「重新跑 `python train_gpt2.py`」。
2. **expected_logits 只分配 `B*T*V`**（`V = model.config.vocab_size`，即 50257），而不是 `B*T*Vp`。因为 PyTorch 的 logits 是 `lnf @ wte.T`，而 `wte` 是未填充的 (V, C)，输出自然只有 V 维。这是后面 logits 比对要「双下标」的根因。
3. **expected_grads 用 `malloc_and_point_parameters` 分配**，于是 `expected_grads.wte`、`expected_grads.qkvw` 等指针自动按正确偏移钉好，可直接与 `model.grads.*` 一一对应。

> 模型本身从 checkpoint 构建，与 debug state 是**两个独立文件**：`gpt2_build_from_checkpoint(&model, "gpt2_124M.bin")` 读权重（参见 [train_gpt2.c:707-763](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L707-L763)），debug state 只承载「输入 + 标准答案」。

#### 4.2.4 代码实践

1. **目标**：确认 debug state 的存在性、大小与魔数。
2. **步骤**：在仓库根目录，先确保已按 README 跑过 `python train_gpt2.py`（它会在 `write_tensors` 分支里生成 `gpt2_124M.bin` 与 `gpt2_124M_debug_state.bin`）。用 `ls -l gpt2_124M_debug_state.bin` 查看大小。
3. **观察**：文件大小应满足 \(1024 + B\cdot T\cdot 4 \cdot 2 + B\cdot T\cdot V\cdot 4 + 4 + \text{num\_parameters}\cdot 4\) 字节（头 + 两个 int32 数组 + logits + loss + 梯度，均按字节展开）。
4. **预期结果**：若文件缺失或魔数不符，`test_gpt2.c` 会在 `fopen` 或魔数校验处直接退出并提示「re-run `python train_gpt2.py`」。
5. 若本地未生成该文件，相关数值结果标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `expected_logits` 用 `B*T*V` 而不是 `B*T*Vp` 分配，而模型内部的 `acts.logits` 却是 `B*T*Vp` 布局？

**答案**：PyTorch 参考的 logits 由未填充的 `wte(V,C)` 经 `lnf @ wte.T` 得到，天然只有 V 维；而 C 实现为了用 cuBLAS 友好的 128 对齐，把词表补到 `Vp=50304`，故 `acts.logits` 按 Vp 步长排布。一个未填充、一个填充，这是后续比对要用两套下标的根因（承接 u2-l6 的 V/Vp 区别）。

**练习 2**：如果某人修改了 `gpt2_backward` 的算法但仍想骗过 debug state 的梯度比对，他能不能通过改优化器超参（lr/wd）来掩盖？

**答案**：不能。debug state 在 `optimizer.step()` 之前产生，梯度只由前向 + 反向决定，与 lr/wd/betas 完全无关。改优化器超参不会改变第 0 步的梯度，因此无法掩盖反向传播的算法 bug。

### 4.3 模块二：前向 logits / loss / 梯度的逐元素比对

#### 4.3.1 概念说明

debug state 比对发生在训练循环的 **step 0**：对同一批 (x, y) 跑一次 `gpt2_forward`（产出 logits 与 mean_loss）和一次 `gpt2_backward`（产出全部 16 个参数梯度），然后与 PyTorch 标准答案逐元素比。这关把「前向」和「反向」分别钉死。

#### 4.3.2 核心流程

step 0 的执行序（与训练四步几乎一致，只是把 `gpt2_update` 推迟到比对之后）：

```text
gpt2_forward(model, x, y, B, T)   ->  model.acts.logits, model.mean_loss
gpt2_zero_grad(model)              ->  清零 grads / grads_acts
gpt2_backward(model)               ->  填充 model.grads.*
if (step == 0) {
    比对 logits   (B*T*V 个元素, 容差 1e-2, 首个超差即判失败并 break)
    比对 loss     (1 个标量,  容差 1e-2)
    比对 16 个梯度张量 (各用 check_tensor, 容差 2e-2)
}
```

`gpt2_zero_grad` 把权重梯度和激活梯度整块 `memset` 为 0（参见 [train_gpt2.c:893-896](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L893-L896)），是反向 `+=` 累加正确的前提。`gpt2_backward` 则先用 \(1/(B\cdot T)\) 填充 `dlosses`「点燃」链式法则（参见 [train_gpt2.c:928-934](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L928-L934)）。

#### 4.3.3 源码精读

**(a) logits 比对：双下标 + 首错即停**

```c
float* calculated_logits = model.acts.logits;
for (int bt = 0; bt < B*T; bt++) {
    for (int v = 0; v < V; v++) {            // 只比到 V，跳过填充区 [V, Vp)
        int i = bt * Vp + v;                  // 模型输出按 Vp 步长
        float diff = fabsf(expected_logits[bt*V + v] - calculated_logits[i]); // 参考按 V 步长
        max_diff = fmaxf(max_diff, diff);
        if (diff >= 1e-2f) {
            printf("MISMATCH AT INDEX %d,%d: ", bt, v);
            logits_ok = 0;
            bt = B*T;                          // 经典 C 技巧：把外层计数器顶到界外，跳出双重循环
            break;
        }
    }
}
```

参见 [test_gpt2.c:113-138](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L113-L138)。

两个关键点：

- **双下标**：`expected_logits[bt*V + v]`（步长 V）对 `calculated_logits[bt*Vp + v]`（步长 Vp）。同一个 (bt, v) 在两个数组里的线性地址不同，因为一个未填充、一个填充——这正是 4.2 里 V/Vp 区别的直接后果。
- **容差 `1e-2`，且首个超差即 `break`**：用 `bt = B*T;` 把外层循环变量顶到界外，从而一次性跳出两层循环（C 没有 `break outer`）。它还顺手记录 `max_diff`，最后打印 `OK (LOGITS), max_diff = %e`。

判定：\( \max_{bt,v}|a - b| < 10^{-2} \) 视为通过。

**(b) loss 比对**

```c
if (fabsf(model.mean_loss - *expected_loss) >= 1e-2) {
    printf("LOSS MISMATCH: %f %f\n", model.mean_loss, *expected_loss);
    allok = 0;
} else {
    printf("LOSS OK: %f %f\n", model.mean_loss, *expected_loss);
}
```

参见 [test_gpt2.c:140-146](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L140-L146)。

`model.mean_loss` 是前向里对 `B*T` 个位置损失求平均得到的标量（参见 [train_gpt2.c:880-891](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L880-L891)）。容差同为 `1e-2`。

**(c) 16 个梯度张量逐一比对**

```c
int gradoks[16];
ParameterTensors grads = model.grads;
gradoks[0]  = check_tensor(grads.wte,      expected_grads.wte,      V*C,      "dwte");
gradoks[1]  = check_tensor(grads.wpe,      expected_grads.wpe,      maxT*C,   "dwpe");
gradoks[2]  = check_tensor(grads.ln1w,     expected_grads.ln1w,     L*C,      "dln1w");
...
gradoks[14] = check_tensor(grads.lnfw,     expected_grads.lnfw,     C,        "dlnfw");
gradoks[15] = check_tensor(grads.lnfb,     expected_grads.lnfb,     C,        "dlnfb");
for (int i = 0; i < 16; i++) { allok = allok && gradoks[i]; }
```

参见 [test_gpt2.c:148-169](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L148-L169)。

注意：

- **顺序与 `write_tensors` 一致**：`gradoks[0..15]` 的检查顺序恰好是 wte, wpe, ln1w, ln1b, qkvw, qkvb, attprojw, attprojb, ln2w, ln2b, fcw, fcb, fcprojw, fcprojb, lnfw, lnfb——与 PyTorch 写出顺序、C 端指针排布顺序三者完全对齐。
- **元素数与张量形状对应**：如 `qkvw` 是 `L*3*C*C`、`fcprojw` 是 `L*C*4*C`，与 `fill_in_parameter_sizes` 里的定义一致。
- **每个张量独立 `check_tensor`**：用 `2e-2` 容差，逐张量打印 `TENSOR OK / NOT OK, maxdiff = %e`，任一不过就把 `allok` 拉低。

> 这关是「点对点」最强约束：logits 验前向、grads 验反向、loss 验聚合。三个一起过，基本能排除单算子的实现错误。但它只看一个点（step 0），还不足以保证「跑起来不发散」——那是下一关的事。

#### 4.3.4 代码实践

1. **目标**：用日志读懂 step 0 的三道比对输出。
2. **步骤**：编译运行 `make test_gpt2 && ./test_gpt2`（前置：根目录有 `gpt2_124M.bin` 与 `gpt2_124M_debug_state.bin`）。重点看 step 0 这一段输出：先是若干对 `expected, calculated` 的 logits 数值，然后是 `OK (LOGITS), max_diff = ...`、`LOSS OK: ...`，以及 16 个 `dwte / dwpe / ... TENSOR OK, maxdiff = ...`。
3. **观察**：注意 `max_diff`/`maxdiff` 的量级。正常情况下它们远小于容差（通常在 `1e-4 ~ 1e-3` 量级），说明 C 与 PyTorch 的数值差异主要来自浮点累加顺序，而非算法。
4. **预期结果**：所有行都带 OK，没有 `MISMATCH` 或 `TENSOR NOT OK`。
5. 若本地无 GPU/无 debug state 文件，输出结果标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：logits 比对用 `diff >= 1e-2` 判失败，梯度比对用 `2e-2`，为什么梯度容差更松？

**答案**：梯度是反向链式法则沿 12 层、多个算子反复 `+=` 累加的结果，浮点累加顺序的差异会被放大；而 logits 是一次前向的末端输出，误差累积路径短。所以给梯度留更大容差（`2e-2`）以容纳合法的数值噪声，同时仍能抓出数量级以上的算法 bug。

**练习 2**：`bt = B*T;` 这一行在双重循环里起什么作用？换成 `break;` 会怎样？

**答案**：它把外层循环计数器直接顶到界外 `B*T`，使外层 `for` 的下一次条件判断为假，从而一次性跳出两层循环——这是 C 里模拟「`break outer`」的惯用法。如果只写 `break;`，只会跳出内层 `v` 循环，外层 `bt` 还会继续遍历剩余位置，已知的 mismatch 之后仍会被反复触发打印。

### 4.4 模块三：10 步训练 loss 回归

#### 4.4.1 概念说明

前两关是「点对点」的，能抓单算子 bug，但抓不到「误差在多步训练中指数放大」这类问题。例如某层反向梯度差了 `1e-3`，单步比对无害通过，但 10 步后 loss 就可能完全偏离。因此第三关把前向+反向+更新串成一个 **10 步训练循环**，把整条 loss 曲线作为指纹回归。

这里有一个反直觉但关键的细节：**这 10 步用的是同一批 (x, y)**。`x`、`y` 只在循环外读一次（见 4.2），循环里每步都喂同一份数据。也就是说模型在**故意过拟合单个固定 batch**，因此 loss 会从 ~5.27 一路单调降到 ~0.38（再往后还会继续逼近 0）。这使 loss 曲线成为一条确定、可复现的「指纹」——任何非确定性的改动都会让它偏离。

#### 4.4.2 核心流程

10 步循环（[test_gpt2.c:101-182](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L101-L182)）：

```text
expected_losses[10] = { 5.2700, 4.0597, 3.3751, 2.8008, 2.3154,
                        1.8490, 1.3947, 0.9991, 0.6241, 0.3765 }  // PyTorch 参考曲线
for step in 0..9:
    gpt2_forward(x, y)        // 用「本步开始时」的权重算 loss -> model.mean_loss
    gpt2_zero_grad()
    gpt2_backward()
    if step == 0: { /* 4.3 的点对点比对 */ }
    gpt2_update(lr=1e-4, b1=0.9, b2=0.999, eps=1e-8, wd=0.01, t=step+1)  // 更新权重
    actual_loss = model.mean_loss                       // 注意：是本步 forward 的 loss
    step_loss_ok = |expected_losses[step] - actual_loss| < 1e-2
    allok = allok && step_loss_ok
    printf("step %d: loss %f ... OK = %d\n", ...)
print("overall okay: %d\n", allok)
```

#### 4.4.3 源码精读

**(a) 参考曲线：硬编码的 10 个 loss**

```c
float expected_losses[10] = {
    5.270007133483887f,
    4.059706687927246f,
    3.3751230239868164f,
    ...
    0.37651097774505615f
};
```

参见 [test_gpt2.c:89-100](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L89-L100)。这 10 个数是从一份 PyTorch 参考训练运行里**原样拷贝**出来的标准 loss 曲线（保留全部有效数字，便于高精度比对）。注意 `expected_losses[0] = 5.2700…` 与 debug state 里的 `expected_loss` 同源——都是初始权重上的那次前向损失。

**(b) 更新调用的超参：注意 wd=0.01**

```c
gpt2_update(&model, 1e-4f, 0.9f, 0.999f, 1e-8f, 0.01f, step+1);
```

参见 [test_gpt2.c:172](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L172)。

对比 `train_gpt2.c` 训练版 `main` 里的更新调用：

```c
gpt2_update(&model, 1e-4f, 0.9f, 0.999f, 1e-8f, 0.0f, step+1);   // wd = 0.0
```

参见 [train_gpt2.c:1168](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1168)。

差别就在**权重衰减**：测试用 `0.01`，正式训练用 `0.0`。原因正是回归测试的本质——`expected_losses` 是从某条特定的 PyTorch 参考曲线拷下来的，C 端要逐位复现它，就必须用与生成该曲线时**相同的优化器超参**。`gpt2_update` 的实现是标准的 AdamW（参见 [train_gpt2.c:1007-1033](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1007-L1033)）：\(t = \text{step}+1\) 从 1 起避免偏差修正除零；`m`/`v` 在首次调用时 `calloc` 清零（懒分配）。

> 小贴士：只有 `expected_losses`（10 步轨迹）依赖优化器超参；4.3 里的 debug state（step 0 的 logits/loss/grads）在 `optimizer.step()` 之前产生，与 lr/wd/betas 无关。所以即使你改了 lr/wd，4.3 仍应通过，但 4.4 的 loss 曲线会偏。

**(c) 比对的「时间语义」：比的是本步更新前的 loss**

```c
gpt2_update(&model, ...);                      // 先更新权重
float expected_loss = expected_losses[step];
float actual_loss = model.mean_loss;           // 但 loss 取自本步「更新前」的 forward
int step_loss_ok = fabsf(expected_loss - actual_loss) < 1e-2;
allok = allok && step_loss_ok;
printf("step %d: loss %f (took %f ms) OK = %d\n", step, model.mean_loss, time_elapsed_s*1000, step_loss_ok);
```

参见 [test_gpt2.c:172-182](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L172-L182)。

注意顺序：`gpt2_update` 在前，取 `model.mean_loss` 在后，但 `model.mean_loss` 是**本步顶部那次 `gpt2_forward`** 写入的值（即用「第 step 步开始时的权重」算出的 loss），`gpt2_update` 不会改写它。因此 `expected_losses[step]` 的语义是「第 step 步开始时（已经过 step 次更新）的损失」：

- step 0：0 次更新（初始权重）→ 5.27（应等于 debug state 的 expected_loss）；
- step 9：9 次更新 → 0.38。

判定式是严格小于：\(|\text{expected\_loss} - \text{actual\_loss}| < 10^{-2}\)。注意它用的是 `<`，而 loss/logits 的 mismatch 判定用的是 `>=`——二者只是把判断方向反过来，阈值同为 `1e-2`。

**(d) 总判定与最终输出**

每关的 `ok` 都用 `allok = allok && ...` 逐道「与」进来（参见 logits 后的 [test_gpt2.c:138](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L138)、loss 的 L143、梯度的 L167-169、每步 loss 的 L178）。循环结束打印总判据：

```c
printf("overall okay: %d\n", allok);
```

参见 [test_gpt2.c:185](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L185)。`allok` 初值为 1（[test_gpt2.c:86](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L86)），任何一关、任何一步失败都会把它清成 0。所以 `overall okay: 1` 是「全部通过」的唯一信号。

#### 4.4.4 代码实践（本讲主实践）

1. **目标**：编译并运行 CPU 版正确性测试，确认 `overall okay: 1`；并解释失败判定。
2. **操作步骤**：
   ```bash
   # 前置：确保根目录已生成 gpt2_124M.bin 与 gpt2_124M_debug_state.bin
   # （由 python train_gpt2.py 的 write_tensors 分支产出）
   make test_gpt2
   ./test_gpt2
   ```
   多核机器可加 `OMP_NUM_THREADS=N ./test_gpt2` 加速（OpenMP 只影响速度，不影响数值）。
3. **需要观察的现象**：
   - 头部打印 `[GPT-2]` 配置、`[State]` 的 B/T、`num_parameters`、`num_activations`；
   - step 0 打印若干对 logits 数值、`OK (LOGITS), max_diff=...`、`LOSS OK: ...`、16 个 `TENSOR OK, maxdiff=...`；
   - step 0..9 各打印一行 `step N: loss ... OK = 1`，loss 从 ~5.27 单调降到 ~0.38；
   - 最后一行 `overall okay: 1`。
4. **预期结果**：所有 `OK = 1`，最终 `overall okay: 1`。各 `max_diff`/`maxdiff` 远小于容差（通常 `1e-4~1e-3` 量级）。若本地暂无 debug state 文件，运行结果标注「待本地验证」。
5. **失败判定说明**（对应本讲规格里的提问）：若**某一步**训练 loss 超过 `1e-2` 容差，即 \(|{\rm expected\_losses[step]} - {\rm model.mean\_loss}| \ge 10^{-2}\)，则该步 `step_loss_ok = 0`，该行打印 `OK = 0`，并通过 `allok = allok && step_loss_ok`（[test_gpt2.c:178](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L178)）把全局 `allok` 拉低为 0；只要 `allok` 在任意一关被清零，最终就打印 `overall okay: 0`，测试即判失败。注意它**不会提前退出**——即便 step 3 失败，仍会继续跑完 10 步并打印每步状态，方便你看到偏离从哪步开始放大。

#### 4.4.5 小练习与答案

**练习 1**：为什么 10 步循环要重复喂同一批 (x, y)，而不是像真实训练那样换不同 batch？

**答案**：为了让 loss 曲线成为确定、可复现的「指纹」。固定 batch + 固定权重初值 + 固定优化器超参 ⇒ 每步 loss 唯一确定，可硬编码为 `expected_losses`。换 batch 会引入数据依赖，曲线不可复现，无法做回归。副作用是模型在过拟合单个 batch，所以 loss 单调降到接近 0——这正是「确定性」的体现。

**练习 2**：如果你把 `gpt2_update` 调用里的 `0.01f`（wd）误改成了 `0.0f`，4.3 和 4.4 分别会怎样？

**答案**：4.3（step 0 的 logits/loss/grads 比对）**仍然通过**，因为 debug state 在优化器之前产生，与 wd 无关。4.4 的 loss 曲线**会偏离**：wd 改变后每步权重更新量不同，loss 轨迹不再等于硬编码的 `expected_losses`，若干步后会超出 `1e-2` 容差，导致 `overall okay: 0`。这说明 4.4 能捕捉优化器超参的回归，而 4.3 不能。

**练习 3**：`expected_losses[0]` 与 debug state 里的 `expected_loss` 数值相同（都是 5.2700…），这是巧合吗？

**答案**：不是巧合。两者都源自「初始权重对同一批 (x, y) 的那次前向损失」：`expected_loss` 是 step 0 forward 后立即记录的，`expected_losses[0]` 是同一次前向在 10 步曲线里的第 0 个点。它们是同一个量的两种存放方式，所以必然相等——你也可以把它当作 4.3 与 4.4 两关之间的一致性自检。

## 5. 综合实践

把三关串起来做一次「故障注入」式的源码阅读：

1. **背景**：假设你给 `attention_backward`（或任一层的反向）引入了一个符号错误，使某个梯度整体差了一个因子。
2. **任务 A（预测）**：先不看运行结果，推断这个 bug 会在哪一关、以什么形式暴露。提示——4.3 的梯度比对会把对应张量打成 `TENSOR NOT OK, maxdiff = ...`（数量级会很大）；4.4 的 loss 曲线会从某步开始加速偏离 `expected_losses`。
3. **任务 B（验证设计）**：在 `test_gpt2.c` 的 step 0 梯度比对段（[test_gpt2.c:148-169](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/test_gpt2.c#L148-L169)）里，找出哪个 `gradoks[i]` 对应你怀疑的那层（对照 `fill_in_parameter_sizes` 的张量顺序），说明如何只看那一行的 `maxdiff` 就能定位到具体参数张量。
4. **任务 C（扩展思考）**：解释为什么「单步比对通过 + 10 步曲线也通过」比「只做单步比对」可信得多——用误差累积的视角说明 4.4 如何放大 4.3 漏掉的微小偏差。
5. **产出**：写一段话总结「debug state 把关前向/反向、expected_losses 把关优化循环」这种**分层正确性策略**，并指出哪一关与优化器超参无关、哪一关有关。

> 本实践为「源码阅读 + 推理」型，无需 GPU；若要真机验证任务 A 的预测，可在本地修改某一层反向并重跑 `./test_gpt2` 观察输出（改完记得还原，不要提交）。运行结果若未本地验证，请如实标注。

## 6. 本讲小结

- `test_gpt2.c` 用 `#define TESTING` + `#include "train_gpt2.c"` 复用全部模型代码，永不与被测代码分叉；`train_gpt2.c` 用 `#ifndef TESTING` 守卫排除训练版 `main`。
- `gpt2_124M_debug_state.bin` 是 PyTorch 在**优化器介入之前**导出的标准答案包（头魔数 `20240327`、版本 2），含 x、y、logits（B·T·V，未填充）、loss、全部 16 个梯度（fp32，顺序与权重一致）。
- **第一关（step 0 点对点）**：logits 用双下标（V 步长 vs Vp 步长）以 `1e-2` 容差比对、首错即停；loss 用 `1e-2`；16 个梯度张量用 `check_tensor` 以 `2e-2` 容差逐一比对。
- **第二关（10 步回归）**：用同一固定 batch 过拟合，把硬编码的 `expected_losses` 曲线作为指纹，每步 \(|\Delta\text{loss}|<10^{-2}\) 才算通过。
- 关键陷阱：测试的 `gpt2_update` 用 `wd=0.01`（正式训练用 `0.0`），是为了逐位复现参考曲线；只有 loss 曲线依赖优化器超参，debug state 不依赖。
- 任一关卡、任一步失败都经 `allok = allok && ...` 累积，最终 `overall okay: 1` 是唯一全过信号；失败时不提前退出，便于看偏离从哪步放大。

## 7. 下一步学习建议

- **横向对照 PyTorch 参考实现**：本讲只把 `train_gpt2.py` 当「标准答案生产者」。想真正理解那 16 个梯度、那条 loss 曲线怎么来的，去读 u4-l1（PyTorch 参考实现），把 `CausalSelfAttention`、`MLP`、`Block` 与 C 的各层逐行对照。
- **吃透二进制协议**：debug state 与权重的 `.bin` 格式细节（版本号、padded vocab、fp32/bf16 写出）在 u4-l2 展开，本讲的「魔数/版本/双下标」都源自那里。
- **走向 GPU 主线**：本讲是 CPU 版测试。CUDA 主线有对应的 `test_gpt2.cu`，分别在 fp32 与混合精度（`USE_CUDNN`）路径下做同样的比对（见 README「test」节）。学完 u5（CUDA 主线）后，可对照 `make test_gpt2cu PRECISION=FP32` 与 `make test_gpt2cu USE_CUDNN=1` 理解 GPU 路径的正确性基线。

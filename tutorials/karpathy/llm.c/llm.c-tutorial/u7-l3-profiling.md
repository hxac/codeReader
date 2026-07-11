# 性能剖析：profile_gpt2cu 与 ncu

## 1. 本讲目标

通过本讲，读者应该能够：

- 理解 llm.c 为什么单独维护一个 `profile_gpt2.cu` 剖析入口，而不是直接剖析 `train_gpt2cu`。
- 看懂 `profile_gpt2.cu` 如何用 `#define TESTING` + `#include` 复用整个训练器、却只跑「恰好一步、单层」的最小训练。
- 掌握 `make profile_gpt2cu` 编译选项里 `-lineinfo` 的作用，以及 `ncu`（NVIDIA Nsight Compute）生成 `.ncu-rep` 报告的标准命令。
- 读懂 `profile_gpt2cu.py` 如何把 `.ncu-rep` 解析成「每个 kernel 的耗时 / 显存带宽 / Tensor Core 利用率」表格，并据此定位性能瓶颈。

## 2. 前置知识

本讲是「专家层」内容，默认你已经读过以下讲义：

- **u5-l1 CUDA 主线架构与 llmc 头文件库**：你需要知道 `train_gpt2.cu` 是 CUDA 主线，`GPT2` 模型结构体、`gpt2_forward` / `gpt2_backward_and_reduce` / `gpt2_update` 等顶层函数都在这一份文件里。

在进入源码之前，先建立三个直觉性的概念：

1. **什么是「剖析（profiling）」？**
   写完 CUDA kernel 后，我们想知道「到底是哪一个 kernel 慢、慢在访存还是计算」。剖析就是用工具给每个 kernel 贴上一张「成绩单」：跑了多久、读写多少字节、Tensor Core 用了几成。llm.c 配套的工具是 NVIDIA 的 **Nsight Compute**，命令行版本叫 **`ncu`**。

2. **`ncu` 为什么会拖慢程序？**
   `ncu` 采集指标的方式是「kernel 重放（replay）」：每个 kernel 会被反复重新执行多次，每次采集一组硬件计数器。这意味着被剖析的程序比正常运行慢一两个数量级。因此我们**只想让程序跑最少、最代表性的工作量**——这是后面「单层、单步」设计的根本动机。

3. **什么是 `.ncu-rep`？**
   它是 Nsight Compute 的二进制报告文件。生成后既可以在带 GUI 的 Nsight Compute 客户端里逐 kernel 查看（作者甚至会把云端跑出的报告 rsync 到 Mac 上看），也可以用 `ncu --csv` 导出成纯文本表格供脚本处理。本讲的 `profile_gpt2cu.py` 走的就是第二条路。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `profile_gpt2.cu` | 剖析专用入口。复用 `train_gpt2.cu` 的全部训练代码，但提供一个只跑「单步单层」的最小 `main`，让 `ncu` 报告小而干净。 |
| `profile_gpt2cu.py` | 解析脚本。调用 `make` 编译、调用 `ncu` 生成报告、再把报告解析成「每个 kernel 的耗时 / 带宽 / 利用率」汇总表，帮助定位瓶颈。 |
| `Makefile` | 提供 `profile_gpt2cu` 编译目标，关键是比普通目标多加了 `-lineinfo`。 |
| `train_gpt2.cu` | 被剖析的「本体」。`profile_gpt2.cu` 通过 `#include` 把它整份纳入；其顶层训练函数（`gpt2_forward` 等）被入口直接调用。 |

## 4. 核心概念与源码讲解

### 4.1 剖析专用入口：profile_gpt2.cu

#### 4.1.1 概念说明

`train_gpt2.cu` 的 `main` 是一个「真实训练器」：它会进入一个训练循环，反复前向-反向-更新成百上千步，中间还穿插验证、采样生成、日志打印。如果直接对它跑 `ncu`，会有两个严重问题：

1. **报告巨大且重复**：12 层 Transformer × 数百步 × 每步几十个 kernel，会产生数百万条 kernel 记录，`.ncu-rep` 文件动辄几十 GB，根本没法看。
2. **耗时被噪声淹没**：训练循环里有数据加载、采样、CPU 端 loss 汇总等与「kernel 性能」无关的代码，它们会混进剖析结果。

`profile_gpt2.cu` 的设计哲学就是：**剖析时只做「最能代表一个训练步」的最小工作量**。它复用了完整的训练器代码（保证剖析的是真实 kernel，而不是简化版），但把 `main` 换成一个极简版本：

- 只跑**一个训练步**（前向 → 反向 → 梯度范数 → 更新）。
- 把模型层数**临时改成 1 层**，因为 12 层的 kernel 是重复的，剖析一层就够。
- 用**假数据**（`i % vocab_size`）填输入，因为 kernel 的耗时只取决于张量形状，不取决于具体数值。
- 禁用多 GPU（`NO_MULTI_GPU=1`），避免 NCCL 通信干扰单卡 kernel 剖析。

#### 4.1.2 核心流程

`profile_gpt2.cu` 的 `main` 执行流程可以概括为：

```
1. 初始化多 GPU 配置（单卡模式）→ common_start（建 CUDA 上下文、cuBLASLt handle）
2. 从 checkpoint 构建 GPT-2 模型（gpt2_124M_bf16.bin）
3. 设置 B=24, T=1024，分配并填充假数据 x、y
4. 关键：把 num_layers 改成 1（剖析一层即可）
5. 分配激活/梯度显存
6. 跑「恰好一步」训练：forward → backward_and_reduce → grad_norm → update
7. cudaDeviceSynchronize（等所有 kernel 完成，保证计时准确）
8. 释放资源
```

这正好覆盖了一个完整训练步里会出现的所有 kernel 类型：encoder、各层前向、分类器（fused_classifier）、各层反向、optimizer（adamw / global_norm）。后面 `profile_gpt2cu.py` 解析时，会把「单层」的耗时再乘以 12，还原成「12 层真实训练步」的估算。

#### 4.1.3 源码精读

**复用训练器代码、却替换 `main` 的技巧**

[profile_gpt2.cu:27-28](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2.cu#L27-L28) 这两行是整个文件的灵魂：

```c
#define TESTING
#include "train_gpt2.cu"
```

先 `#define TESTING`，再 `#include "train_gpt2.cu"`，等于把整份训练器源码「粘贴」进来。而 `train_gpt2.cu` 在自己的 `main` 外面套了一层编译守卫：

[train_gpt2.cu:1353-1354](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1353-L1354) 中 `#ifndef TESTING ... // skip everything below this point`，意味着一旦定义了 `TESTING`，`train_gpt2.cu` 自带的那个真实 `main`（在 [train_gpt2.cu:1419](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1419)）就会被编译器整段忽略。于是链接器看到的 `main` 是 `profile_gpt2.cu` 自己写的这个极简版，避免了「两个 main 冲突」。

这是 llm.c 里反复出现的「同一份代码、多个入口」手法：`test_gpt2.cu` / `test_gpt2.c` 也是同样套路（见 u3-l4）。好处是**剖析/测试用的 kernel 永远和真实训练的 kernel 是同一份**，绝不会因为复制粘贴而分叉。

**初始化与模型构建**

[profile_gpt2.cu:37-43](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2.cu#L37-L43)：

```c
multi_gpu_config = multi_gpu_config_init(...);   // 单卡，nccl_init_method="mpi"
common_start(true, true);                         // 建 CUDA 上下文 + cuBLASLt handle
GPT2 model;
gpt2_init_common(&model);
gpt2_build_from_checkpoint(&model, "gpt2_124M_bf16.bin");
```

`common_start(true, true)` 两个参数对应 `train_gpt2.cu` 里 `void common_start(bool override_enable_tf32 = true, bool print_device_info = true)`（[train_gpt2.cu:1173](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1173)）——开启 TF32 覆盖、打印设备信息。注意这里加载的是 `gpt2_124M_bf16.bin`（BF16 版权重），所以剖析的是 BF16 训练的 kernel。

**假数据填充**

[profile_gpt2.cu:50-55](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2.cu#L50-L55)：

```c
int* x = (int*)mallocCheck(B * T * sizeof(int));
int* y = (int*)mallocCheck(B * T * sizeof(int));
for(int i = 0; i < B * T; ++i) {
    x[i] = i % model.config.vocab_size;
    y[i] = i % model.config.vocab_size;
}
```

用 `i % vocab_size` 填充，纯粹是为了让每个 token id 都落在合法词表范围内。kernel 的执行时间只取决于 `B/T/C` 这些形状参数，与具体数值无关，所以用假数据完全不影响剖析结论——却省去了加载真实 `.bin` 数据的开销。

**「剖析一层就够」的核心技巧**

[profile_gpt2.cu:57-58](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2.cu#L57-L58)：

```c
// override number of layers to 1 because all layers repeat the same kernels, only profile once
model.config.num_layers = 1;
```

注释点明了原因：12 层 Transformer 的每一层执行的 kernel 完全相同（只是数据不同），所以剖析 1 层就能代表 12 层。把层数压到 1，可以让 `.ncu-rep` 里每个 kernel 只出现一次，报告体量缩小约 12 倍，解析也简单得多。后续 `profile_gpt2cu.py` 会把单层耗时乘以 `N_LAYERS=12` 还原成真实训练步的估算。

**恰好一步训练**

[profile_gpt2.cu:63-68](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2.cu#L63-L68)：

```c
gpt2_forward(&model, x, B, T);
gpt2_backward_and_reduce(&model, x, y, 1, 0);
float grad_norm = gpt2_calculate_grad_norm(&model, &multi_gpu_config);
float grad_scale = (grad_norm > 1.0f) ? 1.0f / grad_norm : 1.0f;
gpt2_update(&model, 1e-4f, 0.9f, 0.999f, 1e-8f, 0.0f, grad_scale, 1, &multi_gpu_config);
cudaCheck(cudaDeviceSynchronize()); // finish all CUDA work to get correct precise timings
```

这一段对应 u1-l3 讲过的「前向 → 反向 → 更新」四步，外加一个梯度范数计算（用于梯度裁剪，见 u6-l2）。最后一句 `cudaDeviceSynchronize()` 尤为重要：CUDA kernel 启动是异步的，如果不显式同步，`main` 可能在 kernel 还没跑完时就返回，`ncu` 抓到的计时就不准。

#### 4.1.4 代码实践

**实践目标**：亲手编译剖析入口，确认它能正常跑完一步并退出。

**操作步骤**：

1. 确认仓库根目录下有 `gpt2_124M_bf16.bin`（若没有，运行 `./dev/download_starter_pack.sh` 下载，见 u1-l2）。
2. 在仓库根目录执行：

   ```bash
   make profile_gpt2cu NO_MULTI_GPU=1
   ```

   `NO_MULTI_GPU=1` 关闭 NCCL 多卡支持（剖析单卡 kernel 时不需要）。
3. 直接运行可执行文件（先不接 `ncu`）：

   ```bash
   ./profile_gpt2cu
   ```

**需要观察的现象**：程序会先打印设备信息，然后打印 `batch size: 24` 与 `sequence length: 1024`，最后很快退出（通常几秒内）。

**预期结果**：程序无报错退出，退出码为 0。这一步只验证「剖析入口本身能跑」，尚未产生 `.ncu-rep`。

> 若运行时报 OOM（显存不足），按 `profile_gpt2.cu` 注释提示把 `B` 调小（如 4）。该现象与 GPU 显存大小有关，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `profile_gpt2.cu` 第 58 行的 `model.config.num_layers = 1;` 删掉（保持 12 层），剖析结果会怎样变化？为什么？

**参考答案**：`.ncu-rep` 里每个 Transformer 层 kernel 会出现 12 次，报告体积约扩大 12 倍，但每个 kernel 单次的性能指标（带宽、利用率）几乎不变——因为每层数据形状相同。作者设成 1 层纯粹是为了「报告小、每个 kernel 只出现一次、解析简单」，并不改变对单个 kernel 性能的判断。

**练习 2**：为什么 `profile_gpt2.cu` 要用假数据 `i % vocab_size`，而不是像 `train_gpt2cu` 那样从 `DataLoader` 读真实 token？

**参考答案**：kernel 的耗时只取决于张量形状（`B/T/C`）和访存模式，与具体数值无关。用假数据既省去了加载数据 `.bin` 的 I/O，又避免了剖析过程被数据准备时间污染。真实数据对「正确性」重要，对「性能剖析」无关。

---

### 4.2 ncu 命令与关键指标

#### 4.2.1 概念说明

`ncu`（Nsight Compute CLI）的工作模式是「 attaches 到一个可执行文件，拦截它启动的每个 kernel，反复重放以采集硬件计数器」。理解三个关键概念：

- **指标（metric）**：`ncu` 能采集成百上千种硬件计数器，例如 `gpu__time_duration.sum`（kernel 总耗时）、`dram__bytes_read.sum`（从显存读了多少字节）。完整指标手册见 [Nsight Compute CLI 文档](https://docs.nvidia.com/nsight-compute/NsightComputeCli/)。
- **`--set full`**：采集「非常多」的指标。代价是每个 kernel 重放次数多、剖析慢；好处是报告里几乎什么都有。日常快速排查可以用更小的集合。
- **`-lineinfo` 编译选项**：这是让 `ncu` 能把汇编指令关联回**源码行号**的前提。没有它，报告里只有汇编，看不到「慢在哪一行 C/CUDA 代码」。

#### 4.2.2 核心流程

标准剖析流程是两步走，`profile_gpt2.cu` 顶部注释给出了 TLDR（[profile_gpt2.cu:12](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2.cu#L12)）：

```
sudo ncu --set full --import-source yes -o profile -f ./profile_gpt2cu
```

各参数含义（注释在 [profile_gpt2.cu:14-19](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2.cu#L14-L19)）：

| 参数 | 含义 |
|------|------|
| `--set full` | 采集大量指标（剖析更慢，但信息全） |
| `--import-source yes` | 把源码导入报告，便于在 GUI 里对照源码行 |
| `-o profile` | 输出到 `profile.ncu-rep` |
| `-f` | 强制覆盖已存在的报告文件 |
| `./profile_gpt2cu` | 被剖析的可执行文件 |

生成的 `profile.ncu-rep` 既可在 Nsight Compute GUI 里打开（作者提到会把它从云端 rsync 到本地 Mac 查看，见 [profile_gpt2.cu:22-24](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2.cu#L22-L24)），也可用 `ncu -i profile.ncu-rep --csv` 导出文本。

#### 4.2.3 源码精读

**`-lineinfo` 是 profile 目标的独门佐料**

对比 `Makefile` 里几个 CUDA 目标的编译规则：

[Makefile:273-274](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L273-L274) 是 `train_gpt2cu`，[Makefile:285-286](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L285-L286) 是 `profile_gpt2cu`：

```makefile
train_gpt2cu: train_gpt2.cu $(NVCC_CUDNN)
	$(NVCC) $(NVCC_FLAGS) $(PFLAGS) $^ $(NVCC_LDFLAGS) $(NVCC_INCLUDES) $(NVCC_LDLIBS) $(CUDA_OUTPUT_FILE)

profile_gpt2cu: profile_gpt2.cu $(NVCC_CUDNN)
	$(NVCC) $(NVCC_FLAGS) $(PFLAGS) -lineinfo $^ $(NVCC_LDFLAGS) $(NVCC_INCLUDES) $(NVCC_LDLIBS)  $(CUDA_OUTPUT_FILE)
```

两者几乎一模一样，唯一差别是 `profile_gpt2cu` 多了 `-lineinfo`。`-lineinfo` 让 nvcc 在二进制里保留「源码行号映射」，这是 `ncu` 报告里能看到「这一行 SASS 对应哪一行 `.cu` 源码」的前提。普通训练目标不需要它（会略微增大二进制），但剖析目标必须加。注意它依赖 `$(PFLAGS)`（由 [Makefile:233-244](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L233-L244) 的 `PRECISION` 决定，默认 `-DENABLE_BF16`），所以剖析的是 BF16 kernel。

**ncu 实际采集的指标**

`profile_gpt2cu.py` 在第二步用 `--metrics` 显式列出关心的指标（[profile_gpt2cu.py:36-44](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2cu.py#L36-L44)）：

```python
metrics = [
    "gpu__time_duration.sum",                   # total time
    "dram__bytes_read.sum",                     # DRAM reads
    "dram__bytes_write.sum",                    # DRAM writes
    "lts__t_sectors_srcunit_tex_op_read.sum",   # L2 reads (sectors -- 32B)
    "lts__t_sectors_srcunit_tex_op_write.sum",  # L2 writes (sectors -- 32B)
    "sm__pipe_tensor_op_hmma_cycles_active.avg.pct_of_peak_sustained_active", # % of peak tensor core utilization
    "smsp__inst_executed.sum",                  # instructions
]
```

这张指标表就是 llm.c 判断「kernel 慢在哪」的尺子，可归为三类：

| 类别 | 指标 | 用途 |
|------|------|------|
| 耗时 | `gpu__time_duration.sum` | 排序找最慢的 kernel |
| 访存 | `dram__bytes_*`、`lts__t_sectors_*` | 算 DRAM/L2 带宽，判断是否「访存受限」 |
| 计算 | `sm__pipe_tensor_op_hmma...`、`smsp__inst_executed.sum` | Tensor Core 利用率、指令数，判断是否「计算受限」 |

其中 `lts__t_sectors_*` 的单位是 sector（32 字节），脚本后续会乘以 32 还原成字节（见 4.3.3）。

#### 4.2.4 代码实践

**实践目标**：用 `ncu` 给 `profile_gpt2cu` 生成一份报告，理解每个命令行开关。

**操作步骤**：

1. 确认系统已安装 Nsight Compute（`ncu` 在 PATH 中，或在 `/usr/local/cuda/bin/ncu`）。若没有，安装 CUDA Toolkit 时勾选 Nsight Compute。
2. 运行 `profile_gpt2.cu` 顶部注释给出的命令：

   ```bash
   sudo ncu --set full --import-source yes -o profile -f ./profile_gpt2cu
   ```

   （`sudo` 是因为访问 GPU 性能计数器通常需要 root 权限，见 4.3.1。）
3. 检查生成的报告文件：

   ```bash
   ls -lh profile.ncu-rep
   ```

**需要观察的现象**：终端会滚动打印每个被采样的 kernel 名称（带 `[NVPROF] ...` 或 `... OK` 字样），整个过程比直接运行 `./profile_gpt2cu` 慢得多（可能几分钟）。

**预期结果**：当前目录下生成 `profile.ncu-rep`。若安装了 GUI 版 Nsight Compute，可用 `ncu-ui profile.ncu-rep` 打开查看。

> 是否需要 `sudo` 取决于系统的 `NVreg_RestrictProfilingToAdminUsers` 设置；若报 `ERR_NVGPUCTRPERM`，按 [profile_gpt2cu.py:3-4](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2cu.py#L3-L4) 指引的 NVIDIA 文档放开权限。具体现象**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `make profile_gpt2cu` 比 `make train_gpt2cu` 多了 `-lineinfo`？不加会怎样？

**参考答案**：`-lineinfo` 让 nvcc 在二进制里保留源码行号映射，这是 `ncu` 把汇编指令关联回 `.cu` 源码行的前提。不加它，`ncu` 报告里只能看到机器码/汇编，看不到「慢在哪一行源码」，定位瓶颈会困难得多。训练目标不加是因为它不需要被剖析，且 `-lineinfo` 会略微增大二进制。

**练习 2**：`--set full` 和只采集少量指标相比，代价是什么？

**参考答案**：`--set full` 采集几乎所有指标，每个 kernel 需要被重放更多次，整体剖析时间显著变长（可能慢几倍到几十倍）。好处是报告信息最全，一次剖析就能回答各种问题。快速排查某个具体问题时，可以用 `--metrics` 指定少量指标来加速。

---

### 4.3 瓶颈定位：profile_gpt2cu.py 解析报告

#### 4.3.1 概念说明

`.ncu-rep` 是二进制，直接读不了。`profile_gpt2cu.py` 的作用是把它「翻译」成一张人类可读的表格，并自动算出「哪个 kernel 最耗时、哪个最吃带宽」。它解决三个工程问题：

1. **权限探测**：自动判断当前系统是否允许非 root 用户访问 GPU 性能计数器，决定是否加 `sudo`。
2. **指标导出**：用 `ncu -i profile.ncu-rep --csv --page raw --metrics ...` 把报告导成 CSV 文本。
3. **阶段归类与还原**：因为 `profile_gpt2.cu` 只跑了 1 层，脚本要把每个 kernel 归到「encoder / 前向 / 分类器 / 反向 / 优化器」某个阶段，再把 Transformer 层 kernel 的耗时乘以 12，还原成真实训练步的估算。

最终输出三段：每个 kernel 的明细表、按 kernel 类型的耗时汇总、以及一段「训练步耗时分布 + 整体效率」的文字摘要。

#### 4.3.2 核心流程

脚本的整体流程（对应 [profile_gpt2cu.py:18-46](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2cu.py#L18-L46)）：

```
1. make profile_gpt2cu NO_MULTI_GPU=1 USE_CUDNN=1   # 编译
2. shutil.which("ncu") 定位 ncu                      # 找工具
3. modprobe -c nvidia 探测权限                       # 决定是否 sudo
4. ncu --set full ... -o profile ./profile_gpt2cu    # 生成 .ncu-rep
5. ncu -i profile.ncu-rep --csv --metrics ...        # 导出 CSV
6. 解析 CSV：归类阶段、乘 N_LAYERS、算带宽/利用率      # 瓶颈定位
7. 打印明细表 + 类型汇总 + 文字摘要
```

瓶颈定位的核心数学很简单——对每个 kernel 算出 **DRAM 带宽**和 **Tensor Core 利用率**，再与「全场峰值」对比得到效率：

\[ \text{DRAM BW} = \frac{\text{bytes\_read} + \text{bytes\_write}}{\text{time}} \]

\[ \text{efficiency} = \max\left(\frac{\text{DRAM BW}}{\text{peak DRAM BW}},\ \frac{\text{Tensor util}}{\text{peak Tensor util}}\right) \]

直觉是：**一个理想 kernel 应该要么把显存带宽吃满（访存受限），要么把 Tensor Core 跑满（计算受限）**。`efficiency` 越低，说明这个 kernel 既没吃满带宽也没跑满算力——通常意味着优化空间（比如访存不连续、并行度不足）。

#### 4.3.3 源码精读

**自动定位 ncu 与权限探测**

[profile_gpt2cu.py:12-15](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2cu.py#L12-L15) 先在 PATH 上找 `ncu`，找不到就回退到标准路径：

```python
NCU = shutil.which("ncu")
if NCU is None:
    NCU = "/usr/local/cuda/bin/ncu"
```

[profile_gpt2cu.py:21-22](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2cu.py#L21-L22) 用 `modprobe -c nvidia` 读内核模块配置，看是否含 `NVreg_RestrictProfilingToAdminUsers=0`：

```python
options = subprocess.check_output(["modprobe", "-c", "nvidia"], text=True)
can_profile = len([l for l in options.splitlines() if "NVreg_RestrictProfilingToAdminUsers=0" in l]) != 0
```

若不允许（[profile_gpt2cu.py:29-31](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2cu.py#L29-L31)），自动在命令前加 `sudo`。这是对「`ncu` 报权限错」的预防性处理。

**把单层耗时还原成 12 层**

[profile_gpt2cu.py:51-53](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2cu.py#L51-L53) 定义模型配置：

```python
N_LAYERS = 12
```

在主循环里（[profile_gpt2cu.py:124-150](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2cu.py#L124-L150)），凡是被判定为「Transformer 层 kernel」（即 `phase` 为 `fwd`/`bwd` 等）的，`multiplier` 设为 `N_LAYERS`，并把 time/read/write/inst 都乘以 12：

```python
else:
    pass_name = phase
    multiplier = N_LAYERS
    time *= N_LAYERS
    read *= N_LAYERS
    ...
```

这正是与 `profile_gpt2.cu` 的 `num_layers = 1` 配套的「还原」逻辑：剖析时省了 12 倍，解析时补回 12 倍。

**用 fused_classifier 锚定分类器阶段**

Transformer 最后有一个「分类器」子段（layernorm → matmul → fused_classifier → 反向 matmul ×2 → 反向 layernorm），它不属于「层」、只出现一次。脚本靠找 `fused_classifier` 这个关键字来定位它的起点（[profile_gpt2cu.py:70-74](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2cu.py#L70-L74)）：

```python
if "fused_classifier" in kernel:
    CLS_START = kid - 2
```

然后 [profile_gpt2cu.py:129-131](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2cu.py#L129-L131) 用 `CLS_START <= kid < CLS_START + CLS_NUM` 把这连续 6 个 kernel 归到 `cls` 阶段。这是一种「靠 kernel 名字 + 顺序位置」来切分训练阶段的实用技巧。

**峰值归一化与效率**

[profile_gpt2cu.py:76-93](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2cu.py#L76-L93) 遍历所有 kernel，记录全场最大的 DRAM 带宽与 Tensor Core 利用率，并把峰值 Tensor 利用率规整到 50% 或 100%：

```python
max_tensor = (max_tensor > 50.0) and 100.0 or 50.0
```

注释解释：消费级 GPU 在这个计数器上最多只能达到峰值的 50%，而没有 Tensor Core 的 GPU 则归到 50% 以免除零。这一步让后面的 `efficiency` 百分比有意义。

L2 sector 到字节的换算在 [profile_gpt2cu.py:171-172](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2cu.py#L171-L172)：

```python
l2_read = l2_read * 32 / 1024 / 1024 / 1024   # sector(32B) → GiB
l2_write = l2_write * 32 / 1024 / 1024 / 1024
```

每个 L2 sector 是 32 字节，乘以 32 得字节，再除以 \(1024^3\) 得 GiB。

**最终摘要**

[profile_gpt2cu.py:211-227](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2cu.py#L211-L227) 输出一段文字摘要，把训练步拆成 encoder / forward / classifier / backward / optimizer 五段，并给出整体效率：

```python
summary = f"""
Assuming that every kernel should be either fully DRAM bandwidth or tensor core limited,
... our overall efficiency is {(total['efficiency'] * 100.0 / total_time):.1f}%.
"""
```

这段话点明了 llm.c 性能分析的**核心判据**：理想情况下每个 kernel 都应被「带宽」或「算力」中的某一个限死；整体效率越接近 100%，说明训练越接近硬件极限。这也是定位瓶颈的出发点——先看摘要里哪一段占比异常高，再到明细表里找那一段里效率最低的 kernel。

#### 4.3.4 代码实践

**实践目标**：跑通 `profile_gpt2cu.py`，读懂它输出的三段表格，定位最耗时的 kernel 类型。

**操作步骤**：

1. 确保已安装 `ncu` 且有（或可通过 `sudo` 获得）GPU 性能计数器访问权限。
2. 在仓库根目录执行（脚本会自己编译、自己跑 ncu）：

   ```bash
   python profile_gpt2cu.py
   ```

3. 等待剖析完成（`--set full` 较慢），观察终端输出。

**需要观察的现象**：脚本会依次打印「Kernel calls」明细表（每个 kernel 一行，含 pass、名字、time、RAM BW、tensor%、各档显存读写、指令数）、「Kernel type summaries」（按 kernel 名聚合的耗时与占比）、以及最后的文字 summary。

**预期结果**：

- 明细表里 `cutlass`（cuBLASLt 的 GEMM）、`ampere_bf16`（Tensor Core GEMM）、`cudnn_generated_fort_native_sdpa`（Flash Attention，见 u5-l5）等矩阵乘/注意力 kernel 通常占大头。
- summary 里 `fwd`（前向块）与 `bwd`（反向块）一般占训练步耗时的大头，`opt`（优化器）较小。
- 整体 efficiency 是一个百分数——越高越接近硬件极限。

> 具体数字取决于 GPU 型号与精度设置，**待本地验证**。若报权限错，按脚本第 3-4 行注释链接的 NVIDIA 文档放开 `NVreg_RestrictProfilingToAdminUsers`。

#### 4.3.5 小练习与答案

**练习 1**：脚本为什么要把 Transformer 层 kernel 的耗时乘以 `N_LAYERS=12`，却不给 `fused_classifier` 相关的 kernel 乘 12？

**参考答案**：因为 `profile_gpt2.cu` 把 `num_layers` 设成了 1，剖析时每层 kernel 只跑了 1 次；但真实模型有 12 层，每层都会跑同样的 kernel，所以要把单层耗时乘以 12 还原成真实训练步的估算。而 `fused_classifier` 是模型末端的分类器，整个模型只出现一次（不属于「层」），所以不乘。

**练习 2**：脚本用「全场最大 DRAM 带宽」作为峰值来归一化效率，而不是用 GPU 的标称峰值带宽。这样做的优缺点是什么？

**参考答案**：优点是不需要查 GPU 型号规格表，自适应性更强，能跨 GPU 通用；缺点是「实测最大值」本身可能就低于硬件标称峰值（如果没有任何 kernel 跑满带宽），导致 efficiency 被高估。脚本选择这种「相对峰值」是为了让脚本对任意 GPU 都能开箱运行。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次完整的「编译 → 剖析 → 定位瓶颈」流程：

1. **编译剖析入口**：阅读 `profile_gpt2.cu` 顶部注释（[profile_gpt2.cu:1-25](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/profile_gpt2.cu#L1-L25)），执行 `make profile_gpt2cu NO_MULTI_GPU=1`，对照 [Makefile:285-286](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L285-L286) 说出这条命令比 `make train_gpt2cu` 多了哪个编译开关、为什么需要它。

2. **生成报告**：执行 `profile_gpt2.cu` 注释里的命令 `sudo ncu --set full --import-source yes -o profile -f ./profile_gpt2cu`，得到 `profile.ncu-rep`。记录剖析大约花了多久，体会「kernel 重放」带来的减速。

3. **定位瓶颈**：执行 `python profile_gpt2cu.py`（它会重做第 1-2 步并解析），在输出的「Kernel type summaries」里找出耗时占比最高的 kernel 类型（很可能是 `ampere_bf16` 或 `cutlass` 这类 GEMM），再到明细表里看它的 `RAM BW` 和 `tensor%` 列——判断它到底是「访存受限」还是「计算受限」。

4. **回溯源码**：在 Nsight Compute GUI（或 `ncu -i profile.ncu-rep --print summary`）里找到那个最慢的 kernel，借助 `-lineinfo` 提供的源码映射，定位到它对应的 `llmc/*.cuh` 文件（例如 matmul 对应 `llmc/matmul.cuh`，attention 对应 `llmc/attention.cuh`），结合 u5-l3 / u5-l5 的讲解理解它的优化空间。

**交付物**：一段话总结——「在我的 GPU 上，一个训练步约 X ms，最耗时的 kernel 类型是 Y，它的 DRAM 带宽利用率为 Z%，瓶颈在于（访存 / 计算 / 并行度）」。数字部分若无 GPU 环境则标注「待本地验证」。

## 6. 本讲小结

- llm.c 单独维护 `profile_gpt2.cu` 而非直接剖析 `train_gpt2cu`，是为了让 `ncu` 报告小而干净：用 `#define TESTING` + `#include` 复用全部训练代码，但 `main` 只跑「单步、单层、假数据」。
- `profile_gpt2.cu` 把 `num_layers` 临时改成 1（因为 12 层 kernel 完全重复），`profile_gpt2cu.py` 解析时再把耗时乘以 12 还原——剖析端省、解析端补。
- `make profile_gpt2cu` 比 `make train_gpt2cu` 多了 `-lineinfo`，这是 `ncu` 把汇编关联回源码行的前提。
- 标准 `ncu` 命令是 `sudo ncu --set full --import-source yes -o profile -f ./profile_gpt2cu`，产出 `profile.ncu-rep`。
- `profile_gpt2cu.py` 自动探测 ncu 路径与权限、生成报告、导出 CSV，并按 encoder/前向/分类器/反向/优化器 五段归类，算出每个 kernel 的带宽与 Tensor Core 利用率。
- 瓶颈定位的核心判据是：理想 kernel 应被「显存带宽」或「Tensor Core 算力」之一限死，整体 efficiency 越接近 100% 越好；效率最低的 kernel 就是优化候选。

## 7. 下一步学习建议

- **若你想优化某个具体 kernel**：结合 u7-l1（dev/cuda 内核库）学习 llm.c 如何对单个算子写多版本并 benchmark——`profile_gpt2cu` 帮你「选出最该优化的 kernel」，u7-l1 教你「怎么把那个 kernel 写得更快」。
- **若你想理解被剖析的 kernel 内部**：回到 u5-l3（cuBLASLt MatMul）、u5-l4（layernorm/encoder/gelu/adamw kernel）、u5-l5（Attention 与 cuDNN Flash Attention），这些讲义解释了报告里出现的 kernel 名字背后的实现。
- **若你想剖析多卡通信开销**：本讲用 `NO_MULTI_GPU=1` 关闭了 NCCL。若要剖析多卡场景，可参考 u6-l4（ZeRO 与 NCCL）理解通信 kernel，并改用 NVIDIA Nsight Systems（`nsys`）而非 `ncu`——`nsys` 更适合看「计算与通信的时间线重叠」，而 `ncu` 专注单个 kernel 的微观指标。
- **延伸阅读**：[Nsight Compute CLI 官方文档](https://docs.nvidia.com/nsight-compute/NsightComputeCli/)，以及 `profile_gpt2.cu` / `profile_gpt2cu.py` 自带的注释——它们本身就是最好的使用说明。

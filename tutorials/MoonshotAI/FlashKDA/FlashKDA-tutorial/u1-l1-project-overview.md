# 第 1 讲 · FlashKDA 是什么：项目定位、性能与设计概览

## 1. 本讲目标

读完本讲，你应该能够：

1. 用一两句话说清 FlashKDA 是什么、为谁解决什么问题（高性能 KDA 前向 kernel，替代 `flash-linear-attention` 中的 Triton 版 `chunk_kda`）。
2. 说出它的运行要求：SM90 及以上（Hopper/Blackwell）、CUDA 12.9+、PyTorch 2.4+，并能自己动手检查一台机器是否满足。
3. 复述官方 deep-dive 博客中的四个核心设计决策：chunk 大小为什么选 16、为什么要拆成两个 kernel、数值精度怎么取舍、做了哪些底层优化。
4. 看懂 `BENCHMARK_H20.md` / `BENCHMARK_GB200.md` 两份性能报告：每个数字怎么来的、加速比怎么算、在 H20 上相对 `chunk_kda` 约 1.85×–2.31×。

本讲不要求你写任何 CUDA 代码，重点是把「项目全景图」装进脑子里，为后面逐文件精读打地基。

## 2. 前置知识

本讲会用到的概念，这里用最通俗的语言过一遍。已经熟悉的读者可以跳过。

- **注意力（attention）与线性注意力（linear attention）**：Transformer 依赖注意力机制衡量 token 之间的相关性。标准 softmax 注意力计算量随序列长度 \( T \) 呈平方增长 \( O(T^2) \)。线性注意力把它改写成「维护一个状态矩阵 \( S \)，逐 token 递推更新」的形式，复杂度降为 \( O(T) \)，同时适合推理时的流式解码。KDA 属于这一族。
- **Delta 规则（delta rule）**：普通线性注意力每个 token 只是往状态里「叠加」新信息：\( S_t = S_{t-1} + k_t v_t^\top \)。Delta 规则更进一步：写入前先「减去旧信息中与新值重叠的部分」，即先擦后写。可以类比「先删除旧文件再写入新版本」，而不是一味追加。KDA（Kimi Delta Attention）就是带门控的 delta 规则变体，其逐 token 递推的完整形式是下一讲（u1-l2）的主题。
- **CUTLASS / CuTe**：NVIDIA 开源的 C++ 模板库，用来写高性能 GEMM 类 kernel。CuTe 是其中的布局（layout）描述子系统，让我们用类型系统描述张量的形状、步长和 swizzle。FlashKDA 全部核心代码基于 CUTLASS/CuTe 写成，而不是手写裸指针。
- **SM90 / 计算能力（compute capability）**：NVIDIA GPU 的架构版本号。SM90 = 9.0 架构 = Hopper（如 H20/H100），SM100+ = Blackwell（如 GB200）。SM90 起引入了 TMA（Tensor Memory Accelerator，一种由硬件搬运整块张量的异步拷贝单元）等新特性，FlashKDA 依赖这些特性，所以不支持老显卡。
- **bf16 / fp32**：两种浮点格式。fp32 用 32 位存储（精度高）；bf16 用 16 位（指数位与 fp32 相同、尾数位少，**动态范围大但精度低**）。深度学习推理的主力格式。哪个量用哪种格式存，是本项目的核心设计问题之一。
- **Triton 与 CUDA**：两种写 GPU kernel 的方式。Triton 是 Python 方言、易写但可控粒度粗；CUDA/CUTLASS 是 C++ 底层、难写但能精确控制指令。`flash-linear-attention`（FLA）的 `chunk_kda` 是 Triton 写的，FlashKDA 是 CUDA/CUTLASS 写的——这场「同算法、不同实现」的对比正是本项目的动机。
- **flash-linear-attention（FLA）**：开源线性注意力算子库，提供 `chunk_kda` 等函数。FlashKDA 安装后可被 FLA **自动分发**（auto-dispatch）调用，作为它的加速后端。
- **chunk（分块）与 varlen（变长）**：chunk 化是把长序列切成固定长度的小块（FlashKDA 为 16）用矩阵运算批量处理；varlen 指一个批次里塞多条长短不一的序列（用 `cu_seqlens` 累积长度数组描述），是推理服务的常态。

## 3. 本讲源码地图

本讲主要阅读三份文档型源文件，并用三个代码文件交叉验证文档说法：

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| `README.md` | 项目门面：定位、环境要求、安装、API 表 | 环境要求、安装方式、API 形状约束 |
| `docs/20260420-flashkda-v1-deep-dive.md` | 官方深度博客：v1 的四大设计决策 | chunk 大小 / kernel 融合 / 数值精度 / 底层优化 |
| `BENCHMARK_H20.md` | Hopper H20 上的性能报告 | 表格结构、加速比计算 |
| `BENCHMARK_GB200.md` | Blackwell GB200 上的性能报告 | 与 H20 对照 |
| `flash_kda/__init__.py` | Python 包装层（全项目唯一 Python 文件） | 一次调用发生了什么 |
| `setup.py` | 构建脚本 | 支持的架构列表、架构探测 |
| `csrc/smxx/fwd_launch.cu` | kernel 启动代码（后面讲义精读） | 只看 CHUNK 与两个 grid 两行，验证博客说法 |

> 提示：`csrc/` 下的 C++/CUDA 文件本讲只「远远看一眼」，逐行精读从单元二开始。

## 4. 核心概念与源码讲解

### 4.1 README 导读：FlashKDA 的定位与运行要求

#### 4.1.1 概念说明

README 是了解任何开源项目的第一入口。FlashKDA 的 README 告诉我们三件事：

1. **它是什么**：一句话标语——「Flash Kimi Delta Attention — 基于 CUTLASS 构建的高性能 KDA kernel」。也就是说，它不是模型、不是训练框架，而是一组 **GPU 算子（kernel）**，只负责前向（推理方向）计算。
2. **它跑在哪**：SM90+ 显卡 + CUDA 12.9+ + PyTorch 2.4+。这不是随便写的版本号，而是由它用到的底层特性（TMA、CUTLASS 新 API）决定的。
3. **它怎么被用**：两条路——直接调 Python API `flash_kda.fwd`，或者作为 FLA 的 `chunk_kda` 的自动后端被透明调用。

#### 4.1.2 核心流程

从用户视角看一次完整的使用流程：

```text
安装（3 步）
  git clone → git submodule update --init --recursive（拉取 CUTLASS）→ pip install -v --no-build-isolation .
        ↓ 构建时探测本机 GPU 架构，只编译对应 sm 架构（FLASH_KDA_CUDA_ARCHS=auto/all/列表）
使用（两条路径）
  路径 A：直接调用 flash_kda.fwd(q, k, v, g, beta, scale, out, A_log, dt_bias, lower_bound, ...)
  路径 B：pip install flash-linear-attention>=0.5.0 后调 fla 的 chunk_kda(...)
          → FLA 检测到 FlashKDA 已安装 → 自动分发到 flashkda（可用 FLA_FLASH_KDA=0 退回 Triton）
```

Python 侧极薄：整个项目只有一个 `flash_kda/__init__.py`（42 行）。它做的事仅仅是——按公式向 C++ 侧要一块 workspace 字节缓冲，然后透传所有参数：

- [flash_kda/__init__.py:L34-L38](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L34-L38) —— 从 `q` 读出 `B/T_seq/H`，算出 `T_total = B * T_seq` 与序列数 `N`（有 `cu_seqlens` 时为 `cu_seqlens.numel()-1`，否则为 `B`），再用 `get_workspace_size(T_total, H, N)` 申请一块 uint8 workspace。
- [flash_kda/__init__.py:L40-L41](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L40-L41) —— 调用 pybind11 模块 `flash_kda_C` 导出的 `fwd`，真正的校验、分发、kernel 启动全在 C++ 里。

这一「Python 只做包装、C++ 做一切」的结构，决定了本手册后续讲义的重心几乎都在 `csrc/`。

#### 4.1.3 源码精读

**（1）项目定位与环境要求。** README 开篇给出定位：

- [README.md:L1-L3](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L1-L3) —— 标语：`FlashKDA: Flash Kimi Delta Attention — high-performance KDA kernels built on CUTLASS`。「built on CUTLASS」点明实现技术栈。
- [README.md:L9-L12](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L9-L12) —— 三条硬性要求：**SM90 and above / CUDA 12.9 and above / PyTorch 2.4 and above**。本讲实践任务的检查脚本就是对照这三条。

**（2）安装与架构选择。**

- [README.md:L14-L20](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L14-L20) —— 标准安装三步，注意 `git submodule update --init --recursive` 不可省略（CUTLASS 是子模块）。
- [README.md:L22-L28](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L22-L28) —— 环境变量 `FLASH_KDA_CUDA_ARCHS` 支持 `auto`（默认，探测本机架构）、`all`、或 `90a,100a` 这样的列表。对应构建脚本中：
- [setup.py:L19](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L19) —— `SUPPORTED_CUDA_ARCHS = ["90a", "100a", "103a", "120a"]`，即支持 Hopper（90a）到 Blackwell（100a/103a/120a）四代。
- [setup.py:L22-L28](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L22-L28) —— `detect_cuda_arch()` 用 `torch.cuda.get_device_capability()` 读本机计算能力并映射到架构名。这正是我们实践脚本要做的事的「官方版」。

**（3）作为 FLA 后端。**

- [README.md:L30-L64](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L30-L64) —— 安装 `flash-linear-attention >= 0.5.0` 后，在 `torch.inference_mode()` 下调用 `chunk_kda(...)` 即自动命中 FlashKDA；`FLA_FLASH_KDA=0` 可强制退回 Triton 路径；开 `logging.INFO` 能看到 `[FLA Backend] kda.chunk_kda -> flashkda` 的分发日志。这一集成细节在第 3 单元 u3-l11 展开。

**（4）API 形状约束（重点浏览，不必背）。**

- [README.md:L90-L104](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L90-L104) —— 参数表：`q/k/v/g` 均为 bf16 的 `[B, T, H, K]`/`[B, T, H, V]`；`beta` 是**激活前 logits**（kernel 内部做 sigmoid）；`A_log` fp32 `[H]`、`dt_bias` fp32 `[H, K]`；状态 `initial_state`/`final_state` 形状 `[B, H, V, K]`（batched）或 `[N, H, V, K]`（varlen）。
- [README.md:L106-L109](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L106-L109) —— 三条关键约束：目前要求 `K = V = 128`（head_dim 固定 128，是本项目最重要的架构假设之一）；两个状态张量都给时 dtype 必须一致；给 `cu_seqlens` 时 `B` 必须为 1。

#### 4.1.4 代码实践：编写 check_env.py 环境体检脚本

这是本讲的主实践，把 README 的三条环境要求变成可执行的检查。

**1. 实践目标**：写一个 `check_env.py`，打印 torch 版本、CUDA 版本、GPU 型号与 compute capability，对照 FlashKDA 的三条要求逐项给出 ✓/✗，并输出总结论。

**2. 操作步骤**：

在仓库根目录（或任意位置）新建 `check_env.py`，内容如下（**示例代码**，非项目自带文件，写在仓库外或笔记目录均可，别放进 `csrc/`）：

```python
# check_env.py —— FlashKDA 环境体检（示例代码）
import torch

def parse_ver(v):  # "2.4.0+cu124" -> (2, 4)
    return tuple(int(x) for x in v.split("+")[0].split(".")[:2])

def main():
    results = []

    # 检查 1：PyTorch >= 2.4
    tv = torch.__version__
    results.append(("PyTorch >= 2.4", parse_ver(tv) >= (2, 4), f"{tv}"))

    # 检查 2：CUDA >= 12.9（编译工具链版本）
    cv = torch.version.cuda or "无（CPU 版 torch）"
    results.append(("CUDA >= 12.9",
                    torch.version.cuda is not None and parse_ver(torch.version.cuda) >= (12, 9),
                    f"{cv}"))

    # 检查 3：GPU compute capability >= 9.0（SM90+）
    if torch.cuda.is_available():
        idx = torch.cuda.current_device()
        cap = torch.cuda.get_device_capability(idx)
        name = torch.cuda.get_device_name(idx)
        results.append(("SM >= 90 (Hopper+)", cap >= (9, 0),
                        f"{name}, capability = sm_{cap[0]}{cap[1]}"))
    else:
        results.append(("SM >= 90 (Hopper+)", False, "未检测到可用 CUDA 设备"))

    print(f"{'检查项':<22}{'结果':<6}详情")
    for item, ok, detail in results:
        print(f"{item:<24}{'✓' if ok else '✗':<6}{detail}")

    print("\n结论：", "满足 FlashKDA 运行要求 ✓" if all(r[1] for r in results)
          else "不满足 ✗ —— 请对照 README.md 的 Requirements 一节排查")

if __name__ == "__main__":
    main()
```

运行：`python check_env.py`。

**3. 需要观察的现象**：三行检查结果各自的 ✓/✗；无 GPU 机器上第 3 项应显示「未检测到可用 CUDA 设备」而不是崩溃。

**4. 预期结果**：在一台 H20/H100 机器上，三项全部 ✓；CPU 机器上第 2、3 项 ✗。注意 `torch.version.cuda` 反映的是 **PyTorch 自带的 CUDA 工具链版本**，与 `nvcc --version` 的独立安装版本可能不同——构建 FlashKDA 时用的是后者（由 `CUDA_HOME` 决定，见 [setup.py:L4](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L4)）。具体输出数值取决于你的机器，待本地验证。

**5. 对照官方实现**：`setup.py` 的 `detect_cuda_arch()` 做了同样的能力探测，见 [setup.py:L22-L28](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L22-L28)，可检查你的脚本结论是否与 `python setup.py` 构建时探测到的架构一致。

#### 4.1.5 小练习与答案

**练习 1**：FlashKDA 的三条硬性环境要求分别是什么？为什么不能用 A100（SM80）？
**答案**：SM90+、CUDA 12.9+、PyTorch 2.4+（[README.md:L9-L12](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L9-L12)）。A100 是 SM80，缺少 SM90 引入的 TMA 等硬件特性，且 `setup.py` 的 `SUPPORTED_CUDA_ARCHS`（[setup.py:L19](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L19)）不含 80a，构建根本不会为它生成代码。

**练习 2**：提供 `cu_seqlens` 时，对 `B` 和状态张量形状有什么约束？
**答案**：`B` 必须为 1，`T` 表示所有序列的总长度；`initial_state`/`final_state` 形状为 `[N, H, V, K]`（N 为序列条数）。不提供时每个 batch 元素是一条独立序列，状态形状 `[B, H, V, K]`（[README.md:L106-L109](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L106-L109)）。

**练习 3**：为什么 `flash_kda.fwd` 每次调用都要新建 workspace，而不是复用一个全局的？
**答案**：workspace 尺寸由 `get_workspace_size(T_total, H, N)` 决定（[flash_kda/__init__.py:L38](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L38)），随序列总长、head 数、序列条数变化；调用方各按当次形状分配，既避免越界也避免为最大形状常驻一块大显存。

### 4.2 deep-dive 博客四大设计决策

#### 4.2.1 概念说明

[docs/20260420-flashkda-v1-deep-dive.md](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md) 是官方对 v1 版本的复盘，篇幅只有 87 行，却是理解整个代码库的「地图」。它回答四个问题：

| # | 问题 | 决策 |
|---|---|---|
| 1 | chunk 选多大？ | `CHUNK = 16`（FLA 用 64） |
| 2 | 融合成几个 kernel？ | 拆成 2 个：K1（token 并行）+ K2（head 并行） |
| 3 | 数值精度怎么存？ | 片上状态存 bf16，关键路径 fp32 累加 |
| 4 | 还做了什么底层优化？ | ex2 换底、K1 occupancy、K2 寄存器内转置 |

为什么值得先读它？因为这份代码里大量「看起来奇怪」的写法（为什么偏偏 16？为什么拆两个 kernel？为什么状态用 bf16？）都能在这 87 行里找到动机。先懂「为什么」，再读「怎么做」，阅读效率高得多。

#### 4.2.2 核心流程

四大决策的逻辑链可以串成一句话：**「chunk 定小（16）→ 数值范围与求逆成本都可控 → 但小 chunk 递推并行度低，所以按并行轴拆两个 kernel → 再用精度取舍和指令级优化把每个 kernel 压榨到极限。」**

决策 1（chunk = 16）的三条理由（[deep-dive 第 1 节](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L11-L26)）：

- **数值范围落入 bf16**：门控 `lower_bound = -5` 时，`CHUNK = 16` 让 `exp(cumsum(g))` 的范围保持在 bf16 可表示精度内，从而**不需要**大 chunk 必须配备的复杂的块内 rescaling 技巧；
- **求逆便宜**：16×16 求逆远便宜于 64×64，可直接用前代换（forward substitution）完成，无需进一步分解；
- **只依赖 SM80 MMA 指令**：所有 16 尺寸的矩阵乘都能映射到 SM80 就有的 MMA 指令，不绑死新架构特性，可移植性好。

决策 2（双 kernel 划分，[deep-dive 第 2 节](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L28-L43)）：

```text
K1（token 并行，grid = N × H × num_chunks）
    g 激活 → L2 归一化 → decay 应用 → L/Mqk 构造 → 16×16 矩阵求逆
        ↓ 中间结果写入 global workspace
K2（仅 head 并行，grid = N × H）
    逐 chunk 的 delta 规则递推 → 输出投影 → 状态累积
```

早期原型是单个融合 kernel：token 并行的 K1 部分被并行度低得多的递推部分拖住，大量 SM 空转。拆开后**端到端至少提速 15%**，且两个 stage 可以独立调参。

决策 3 与决策 4 的要点见下方源码精读。

#### 4.2.3 源码精读

**（1）chunk 大小 = 16 在代码中的落点。** 博客的说法对应三处 `CHUNK = 16`：

- [csrc/smxx/fwd_launch.cu:L31](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L31) —— 启动代码里的 `constexpr int CHUNK = 16;`；
- [csrc/smxx/fwd_kernel1.cuh:L5](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L5) 与 [csrc/smxx/fwd_kernel2.cuh:L9](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L9) —— 两个 kernel 的模板默认参数 `template <int D, int CHUNK = 16>`。

**（2）双 kernel 划分在代码中的落点。** 两个 grid 的并行轴正如博客所述：

- [csrc/smxx/fwd_launch.cu:L169](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L169) —— `dim3 grid_k1(total_tiles, H);`：K1 按「chunk tile 数 × head」铺满 grid，token/chunk 维度充分并行；
- [csrc/smxx/fwd_launch.cu:L203](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L203) —— `dim3 grid_k2(N, H);`：K2 只有「序列 × head」，每个 block 内部沿时间逐 chunk 串行递推——这正是并行度不对等的来源，也是拆分的理由。
- 两个 kernel 的名字：`_flash_kda_fwd_prepare`（K1，[csrc/smxx/fwd_kernel1.cuh:L120](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L120)）与 `_flash_kda_fwd_recurrence`（K2，[csrc/smxx/fwd_kernel2.cuh:L133](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L133)）。「prepare（准备）/ recurrence（递推）」的命名即分工。

**（3）数值精度决策。** [deep-dive 第 3 节](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L45-L71) 的要点：

- 片上递推状态存 **bf16**：状态占用 smem 减半，并消除每个喂给状态的 GEMM 关键路径上的 fp32→bf16 转换；只要状态**更新本身用 fp32 FMA**，两次更新之间以 bf16 存储累积器，内部测试未测得精度损失；
- sigmoid 用 PTX `tanh.approx.f32` 实现：更快且对门控路径足够精确；
- 16×16 求逆**精确计算**：种子 L 全程 fp32（L 的 GEMM 保留原始 fp32 累加器、tril/beta 掩码在 fp32 中做），两个对角 8×8 块用 fp32 前代换求逆，非对角块用两次 bf16 HMMA（`dc = P @ M`、`o = (−dc) @ P`，均为 fp32 累加）合并。（这一求逆器是全项目最精巧的部件之一，u3-l1 整讲 devoted to 它；git 提交 `7afb9f4` 显示它取代了数值上会灾难性抵消的 fp16 Neumann 级数方案。）

**（4）其他优化。** [deep-dive 第 4 节](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L73-L87)：

- **换底到 2 的指数**：`g_act` 阶段把指数换底为 2、用 `ex2.approx.ftz.f32`，省掉换底 FMA 且 `ex2` 吞吐高于 `exp`；
- **K1 occupancy**：靠「生命周期不重叠的 smem 缓冲做 union 复用」+ `__launch_bounds__(256, 8)`，用少量寄存器溢出换取每 SM 明显更多 thread block。`__launch_bounds__` 可直接在代码里看到：[csrc/smxx/fwd_kernel1.cuh:L120](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L120) 的 `__launch_bounds__(NumThreads, 8)`；
- **K2 寄存器内转置**：用 `MOVM_T` 指令直接在寄存器文件里转置操作数，消除 stage 之间全部 smem 往返，同时缩小 K2 的 smem 需求。

#### 4.2.4 代码实践：把博客的四条论断锚定到源码行

**1. 实践目标**：用文本搜索验证「博客里说的每件事，代码里真的有」，建立文档 ↔ 代码的对应感。

**2. 操作步骤**：

1. 在仓库根目录执行（示例命令，可在终端运行）：
   ```bash
   grep -rn "CHUNK = 16" csrc/          # 应在 fwd_launch.cu / flash_kda.cpp 等多处命中
   grep -n "dim3 grid_k" csrc/smxx/fwd_launch.cu   # 两个 grid 定义
   grep -n "launch_bounds" csrc/smxx/*.cuh          # 两个 kernel 的 launch_bounds
   ```
2. 对每条命中，打开文件看上下文 5 行，把「文件:行号 → 博客哪一节」记进你的笔记，格式如 `fwd_launch.cu:169 ↔ deep-dive §2 K1 grid`。
3. 再执行 `git log --oneline -5` 看最近提交，确认 HEAD 提交 `7afb9f4` 的主题（fp32 前代换求逆取代 fp16 Neumann 逆）与 deep-dive §3 的求逆描述吻合。

**3. 需要观察的现象**：`CHUNK = 16` 至少在 3 个文件出现；两个 grid 的维度形式分别是 `(total_tiles, H)` 与 `(N, H)`。

**4. 预期结果**：得到一张约 6-8 行的「博客论断 ↔ 源码行」对照表。这是后续所有讲义反复使用的锚定方法。具体命中行数以本机 grep 输出为准（待本地验证，本讲引用的行号已对 HEAD `7afb9f4` 校验）。

#### 4.2.5 小练习与答案

**练习 1**：用一句话解释为什么 `CHUNK = 16` 能省掉「块内 rescaling 技巧」。
**答案**：chunk 越小，一个 chunk 内门控对数累积和 `cumsum(g)` 的绝对值越小（`lower_bound = -5` 时 16 步内约几十量级），`exp(±cumsum)` 仍在 bf16 的可表示范围内；chunk 取 64 时累积和扩大 4 倍，`exp` 结果会溢出/下溢，必须引入逐块 rescaling。定量推导在第 3 单元 u3-l8 展开。

**练习 2**：早期单 kernel 原型为什么慢？拆分带来了多少加速？
**答案**：token 并行的准备阶段与并行度低得多的递推阶段耦合在同一个 grid 里，递推成为瓶颈、大量 SM 空闲；拆成 K1/K2 后端到端至少提速 15%，且两个 stage 可独立调参（[deep-dive §2](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L39-L43)）。

**练习 3**：状态存 bf16 为什么「不掉精度」？
**答案**：精度关键在状态**更新**的那一步 FMA 用的是 fp32；两次更新之间以 bf16 存储累积器只影响表示舍入，内部推理基准上未测得精度损失（[deep-dive §3](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L46-L55)）。同时换来 smem 占用减半、GEMM 关键路径上少一次类型转换。

### 4.3 性能基准表解读

#### 4.3.1 概念说明

`BENCHMARK_H20.md` 与 `BENCHMARK_GB200.md` 是两份机器生成的性能报告（由 `benchmarks/generate_benchmark_md.py` 产出），分别对应 Hopper 架构 H20 与 Blackwell 架构 GB200。读表前先明确三个概念：

- **mean (ms)**：该 case 在 `warmup=30, iters=200, repeats=5` 设置下多次重复测量的平均耗时（毫秒）。数值越小越快。
- **Speedup（加速比）**：**对手的 mean ÷ FlashKDA 的 mean**。例如 4.8388 ÷ 2.6220 ≈ 1.85，即快 1.85 倍。
- **公平性配置**：表头列出了 `fla_chunk_kda` 的对齐配置（`use_gate_in_kernel=True`、`use_qk_l2norm_in_kernel=True`、`use_beta_sigmoid_in_kernel=True`、`lower_bound=-5`、`transpose_state_layout=True`），保证两边做**完全相同语义**的计算（门控、L2 归一化、beta sigmoid 都在 kernel 内完成），比的是纯实现效率。

> 注意：两份报告同时对比了 `fla_chunk_gdn`（chunked gated delta rule，一种每 head 标量门控的简化亲戚），FlashKDA 对它的加速幅度较小（H20 上约 1.17×–1.43×），这属于「捎带对比」，主要对手是 `chunk_kda`。

#### 4.3.2 核心流程

读一张表的步骤：

```text
1. 看表头上方的形状标题（如 T=8192, H=96, D=128）
2. 看 Case 列：Fixed（单条定长序列）还是 Varlen（变长多序列，附 seq_lens）
3. 看 flash_kda mean 与 fla_chunk_kda mean 两列
4. 心算除法验证 Speedup 列
5. 对比不同 Case：varlen 切得越碎/越规整，加速比如何变化
```

从 H20 报告可归纳出两个规律（下节源码精读给出数字）：

- FlashKDA 在**所有 case** 上都快于两个对手；
- **varlen 场景优势更大**，且「`1024 × 8`」这种规整切分比「乱长切分」优势更明显——varlen 恰是真实推理服务的常态，这是本项目实用价值的核心证据。

#### 4.3.3 源码精读

**（1）测量设置与公平性配置。**

- [BENCHMARK_H20.md:L7-L10](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/BENCHMARK_H20.md#L7-L10) —— `warmup=30, iters=200, repeats=5`；以及两个对手的完整参数配置。warmup 排除首次启动/JIT 编译噪声，iters/repeats 降低计时抖动。
- [BENCHMARK_GB200.md:L7-L10](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/BENCHMARK_GB200.md#L7-L10) —— GB200 报告同样的设置结构，便于跨架构对比。

**（2）H20 主表（T=8192, H=96, D=128）。**

- [BENCHMARK_H20.md:L14-L18](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/BENCHMARK_H20.md#L14-L18) —— 三行数据：Fixed 1.85×；乱长 varlen（`[1300, 547, 2048, 963, 271, 3063]`，总长 7192）2.06×；规整 varlen（`1024 × 8`，总长 8192）2.29×。对照 `chunk_gdn` 分别为 1.22×/1.30×/1.43×。

**（3）H20 次表（T=8192, H=64, D=128）。**

- [BENCHMARK_H20.md:L22-L26](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/BENCHMARK_H20.md#L22-L26) —— 加速比 1.95×/1.91×/2.31×。合并两表：H20 上相对 `chunk_kda` 的加速区间为 **1.85×–2.31×**（学习目标里「约 1.85–2.3 倍」的出处）。

**（4）GB200 对照表。**

- [BENCHMARK_GB200.md:L14-L18](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/BENCHMARK_GB200.md#L14-L18) 与 [BENCHMARK_GB200.md:L22-L26](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/BENCHMARK_GB200.md#L22-L26) —— Blackwell 上加速区间 1.70×–3.27×，varlen 规整切分时最高（3.21×–3.27×）；但 H=64 Fixed 一案对 `chunk_gdn` 只有 0.96×（略慢于 gdn）。提示：跨架构、跨对手的对比要整表看，不要只挑一行。

#### 4.3.4 代码实践：手工复算加速比，验证表格自洽

**1. 实践目标**：用几行 Python 把两份报告的 Speedup 列全部重算一遍，确认「加速比 = 对手 mean ÷ flash_kda mean」，并统计出本讲的结论区间。

**2. 操作步骤**：

新建 `speedup_check.py`（**示例代码**）：

```python
# speedup_check.py —— 复算 BENCHMARK 报告的 Speedup 列（示例代码）
rows = [  # (case, flash_kda, chunk_kda, gdn)  数据摘自 BENCHMARK_H20.md 两张表
    ("H96 Fixed",        2.6220, 4.8388, 3.1985),
    ("H96 Varlen-mix",   2.3449, 4.8291, 3.0541),
    ("H96 Varlen-1024x8",2.0432, 4.6723, 2.9117),
    ("H64 Fixed",        1.6217, 3.1659, 2.0062),
    ("H64 Varlen-mix",   1.7060, 3.2551, 1.9986),
    ("H64 Varlen-1024x8",1.3951, 3.2175, 1.9568),
]
sp = []
for case, fk, kda, gdn in rows:
    s = kda / fk
    sp.append(s)
    print(f"{case:<20} vs chunk_kda: {s:.2f}x   vs gdn: {gdn/fk:.2f}x")
print(f"\nH20 加速区间: {min(sp):.2f}x ~ {max(sp):.2f}x")
```

**3. 需要观察的现象**：每行算出的加速比与报告 Speedup 列四舍五入后一致；区间打印为 1.85–2.31。

**4. 预期结果**：6 行全部吻合（例如 4.8388/2.6220 = 1.845… ≈ 1.85，4.6723/2.0432 = 2.288… ≈ 2.29），说明报告的 Speedup 列就是 mean 之比，无其他修正因子。可再把 `BENCHMARK_GB200.md` 的 12 个数字抄进来复算（区间应为 1.70×–3.27×）。

#### 4.3.5 小练习与答案

**练习 1**：H20 报告中 FlashKDA 相对 `fla_chunk_kda` 的加速区间是多少？哪个 case 最快？
**答案**：1.85×–2.31×；最快是 H=64 的 `1024 × 8` varlen case（2.31×）。规整 varlen 切分让尾块浪费最小、K1 的 tile 填充率最高，优势得以完全兑现。

**练习 2**：为什么报告要明确写出对手 `fla_chunk_kda` 的 5 项配置？
**答案**：保证语义对齐——`use_gate_in_kernel/use_qk_l2norm_in_kernel/use_beta_sigmoid_in_kernel=True` 等设置让门控激活、L2 归一化、beta sigmoid 都在 kernel 内完成，与 FlashKDA 的计算范围一致（FlashKDA 的 `beta` 参数即「激活前 logits、kernel 内 sigmoid」，见 [README.md:L96](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L96)）。否则「把预处理推给 PyTorch」能制造虚假加速。

**练习 3**：GB200 报告里哪一格提醒我们「FlashKDA 并非处处最快」？
**答案**：[BENCHMARK_GB200.md:L24](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/BENCHMARK_GB200.md#L24) H=64 Fixed 一行：对 `fla_chunk_gdn` 只有 0.96×（0.9247 ms vs 0.8857 ms，略慢）。KDA 比 gdn 多每维门控与求逆开销，在小 head 数定长场景可能被追平；但同表内对 `chunk_kda`（同算法对手）仍 1.70×。

## 5. 综合实践

**任务：产出一份「FlashKDA 环境与名片」报告。** 把本讲三个模块串起来，输出一页 Markdown 笔记（建议放 `FlashKDA-tutorial/notes/` 或你自己的笔记目录，不要放进 `csrc/`）：

1. **环境体检**：运行 4.1.4 的 `check_env.py`，把输出贴进笔记并写结论（本机能否跑 FlashKDA；不能的话差哪一条）。
2. **源码锚定**：完成 4.2.4 的 grep 对照表，至少包含 6 条「博客论断 ↔ 源码行号」。
3. **数据复算**：运行 4.3.4 的 `speedup_check.py`，写出 H20 与 GB200 两个加速区间。
4. **一句话名片**：用不超过 3 句话向同事介绍 FlashKDA——它是什么（CUTLASS 实现的 KDA 前向 kernel）、核心设计（CHUNK=16 + 双 kernel 流水 + bf16 状态）、收益（H20 上对 chunk_kda 约 1.85×–2.31×，varlen 更快）。

完成标准：三项输出齐全，且「名片」中的每个数字都能在报告或源码中找到出处。

## 6. 本讲小结

- FlashKDA 是 MoonshotAI 开源的**高性能 KDA（Kimi Delta Attention）前向 kernel**，基于 CUTLASS/CuTe 实现，目标是替代 FLA 中 Triton 版 `chunk_kda`；运行要求 SM90+、CUDA 12.9+、PyTorch 2.4+。
- Python 层极薄：`flash_kda/__init__.py` 只负责按 `get_workspace_size(T_total, H, N)` 申请 workspace 并透传参数，其余全在 C++。
- 四大设计决策：`CHUNK = 16`（bf16 数值范围可控 + 便宜的前代换求逆 + SM80 MMA 可移植）；按并行轴拆 **K1 prepare（grid = tiles × H）/ K2 recurrence（grid = N × H）** 双 kernel（拆分带来 ≥15% 端到端提速）；**片上状态 bf16 + 更新路径 fp32** 的精度分工；外加 ex2 换底、K1 occupancy、K2 寄存器内转置三项底层优化。
- 性能报告的读法：Speedup = 对手 mean ÷ FlashKDA mean；表头的对手配置保证语义对齐。H20 上对 `chunk_kda` 约 **1.85×–2.31×**，varlen 场景优势更大；GB200 上 1.70×–3.27×。
- 阅读 CUDA 项目的有效方法：**先读官方设计文档，把每条论断用 grep 锚定到源码行**，再进入逐行精读——这是本手册后续所有讲义的套路。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：补上本讲刻意跳过的数学——KDA 的逐 token 递推公式、门控 `g = lower_bound * sigmoid(exp(A_log) * (g_raw + dt_bias))` 的构造、beta 与 L2 归一化的角色，对照 `tests/torch_ref.py`。这是读懂一切 kernel 细节的前提。
- **再下一讲（u1-l3）**：动手从零构建项目（submodule、`FLASH_KDA_CUDA_ARCHS`、`tests/test.sh`），跑通第一个正确性测试。
- **提前浏览（不求甚解）**：`csrc/smxx/fwd_launch.cu` 的两个 `dim3 grid_*`（[L169](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L169)、[L203](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L203)），为 u1-l4 的调用链地图留下印象。
- 若你想先看「为什么值得学」，可回看 [BENCHMARK_H20.md](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/BENCHMARK_H20.md) 里 varlen 的三个 case——真实推理负载正是 FlashKDA 相对 Triton 实现优势最大的地方。

# 二次开发实践：消融、调参与扩展方向

## 1. 本讲目标

前 11 讲我们把 FlashKDA 的两个 kernel 逐行读完。本讲换一个视角：**把代码当作可以被改造的对象**。读完后你应该能够：

1. 列出项目中所有可以安全实验的编译期旋钮（`BLOCK_LEVEL_K1`/`BLOCK_LEVEL_K2`/`TMA_DISABLE_ALL`/`kInputStages`/`kOutputStages`/`PREFETCH`/`__launch_bounds__` 等）及各自的风险。
2. 亲手完成一次结构消融实验：编译四个版本（基线、去 K1、去 K2、去 TMA 流水线），用测试与基准量化每个流水线阶段的贡献。
3. 评估「支持 D=64 或新的 head_dim」的真实工作量——它不是改一个模板参数，而是 host 校验、显式实例化、K1 线程映射、K2 warp 分工、workspace 契约与 bit-exact 参考实现的**全链改造**。
4. 形成一套可复用的二次开发流程：**改一处 → 重编译 → exact-match 回归 → benchmark → 记录**。

---

## 2. 前置知识

**消融实验（ablation study）**：像做外科手术一样，有目的地拆掉系统的一个部件，观察系统行为（正确性、耗时）的变化，从而量化这个部件的贡献。FlashKDA 把消融点做成了**预处理器宏**——编译期裁剪，零运行时开销，但代价是每个消融版本都要单独编译安装一次。

**编译期旋钮 vs 运行时旋钮**：
- 编译期旋钮：宏（`-DBLOCK_LEVEL_K1=-1`）、模板参数（`D`、`InputStages`）、`__launch_bounds__`。改了要重编译，每个取值生成一份独立代码。
- 运行时旋钮：API 参数（`scale`、`lower_bound`、状态 dtype、varlen 划分）。不重编译即可调，但能改变的东西由编译期代码预先固定。

**`#ifndef` 守卫与命令行 `-D` 的优先级**：源码里 `#ifndef X / #define X 1 / #endif` 的意思是「只有当 X 尚未被定义时才给默认值」。nvcc 命令行上的 `-DX=...` 在编译一开始就已定义了 X，因此**命令行注入永远覆盖源码默认值**——这是我们不修改源码就能消融的原理。

**可编辑安装的重建机制**：`pip install -e .` 产物是源码树里的 `flash_kda_C` 扩展模块。重新执行同一条命令时，构建系统按时间戳判断是否重编；改的是 `.cuh` 头文件时建议先 `touch csrc/smxx/fwd_launch.cu`（唯一的翻译单元）强制重编（参见 u1-l3）。

**软件流水线与 occupancy（承接 u3-l2/u3-l3）**：K2 用 `PipelineTmaAsync`（深度 3）与 `PipelineAsync`（深度 2）两级流水线；K1 靠 `__launch_bounds__(256, 8)`（每 SM 至少驻留 8 个 CTA）用海量并行块隐藏延迟。调流水线深度改的是 smem 占用，调 launch bounds 改的是寄存器预算与 occupancy 的交换。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [csrc/smxx/utils.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh) | 公共工具 | `BLOCK_LEVEL_K1/K2` 默认定义、`WorkspaceSizes` 对齐约束 |
| [csrc/smxx/fwd_launch.cu](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu) | 唯一 `.cu` 翻译单元 | 两个 kernel 的 `#if` 启动块、`kInputStages/kOutputStages/CHUNK`、线程数、显式实例化表 |
| [csrc/smxx/fwd_kernel1.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh) | Kernel 1（prepare） | 消融后谁来写 workspace；扩展 D=64 时会越界的线程映射 |
| [csrc/smxx/fwd_kernel2.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh) | Kernel 2（recurrence） | `TMA_DISABLE_ALL` 的全部裁剪分支、`PREFETCH`、warp 列块假设 |
| [csrc/flash_kda.cpp](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp) | pybind 入口 | `D == 128` 硬校验、`launch_fwd<128, ...>` 字面量 |
| [setup.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py) | 构建脚本 | nvcc 旗标（fast_math、ptxas -v）、`NVCC_THREADS`、架构选择 |
| [tests/torch_ref.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py) | bit-exact 参考 | `CHUNK = 16` 硬编码——任何数值改动都要同步它 |
| [benchmarks/bench_fwd.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py) | 基准脚本 | CLI 参数、fixed/varlen 用例 |

---

## 4. 核心概念与源码讲解

### 4.1 编译期消融开关

#### 4.1.1 概念说明

FlashKDA 内置了三个**结构级消融开关**，全部通过预处理器裁剪代码：

| 宏 | 默认值 | 关闭方式 | 裁掉的东西 |
| --- | --- | --- | --- |
| `BLOCK_LEVEL_K1` | `1` | 传负值（如 `-1`） | 整个 Kernel 1 的启动块（含 varlen 前缀和 kernel） |
| `BLOCK_LEVEL_K2` | `1` | 传负值（如 `-1`） | 整个 Kernel 2 的启动块 |
| `TMA_DISABLE_ALL` | 未定义 | `-DTMA_DISABLE_ALL` | K2 的 load/store warp、两条流水线与全部状态 TMA 路径 |

要点：这些开关是**计时用的骨架开关，不是功能回退**。任何非基线组合都会产出错误结果——这正是消融实验的意义（量化贡献），但意味着消融版本跑测试**预期失败**。

#### 4.1.2 核心流程

以「关闭 K1」为例的编译期裁剪流程：

1. 构建时向 nvcc 传入 `-DBLOCK_LEVEL_K1=-1`（命令行定义优先于源码默认）。
2. 预处理器求值 `#if BLOCK_LEVEL_K1 >= 0` → `-1 >= 0` 为假 → K1 的整个启动块不进入编译产物。
3. `fwd` 的 Python/C++ 调用路径不变，只是 workspace 永远没人写；K2 读到的是 `torch.empty` 分配的**未初始化显存**（[flash_kda/__init__.py:38](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/flash_kda/__init__.py#L38)），输出为垃圾值。
4. 端到端耗时 ≈ K2 单 kernel 的耗时 → 与基线相减即得 K1 的贡献。

⚠️ **最容易踩的坑**：`#if` 条件是 `>= 0` 而不是 `> 0`，所以传 `-DBLOCK_LEVEL_K1=0` **关不掉任何东西**（`0 >= 0` 成立，kernel 照常编译启动）。关闭必须传负值。读消融宏时永远先看 `#if` 的比较方向。

#### 4.1.3 源码精读

**① 默认值定义（utils.cuh 顶部）**——两个 block 级开关的默认值在这里：

[utils.cuh:30-36](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L30-L36)：`#ifndef BLOCK_LEVEL_K1 / #define BLOCK_LEVEL_K1 1`（K2 同理）。`#ifndef` 守卫使命令行 `-D` 注入可以覆盖默认值；`TMA_DISABLE_ALL` 则相反——源码里只有一行被注释掉的定义，靠 `#ifndef` 分支裁剪。

**② K1 启动块整体被 `#if` 包住（fwd_launch.cu）**：

[fwd_launch.cu:146-181](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L146-L181)：第 147 行 `#if BLOCK_LEVEL_K1 >= 0` 守住「设置动态 smem →（varlen 时）启动前缀和 kernel → 启动 `_flash_kda_fwd_prepare`」的全部代码。注意 [第 164-167 行](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L164-L167) 的 `_flash_kda_build_tile_prefix` 也在这块里——关闭 K1 时 varlen 的 tile 前缀和也一并消失。

**③ K2 启动块对称地被守住**：

[fwd_launch.cu:183-216](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L183-L216)：第 184 行 `#if BLOCK_LEVEL_K2 >= 0`。关闭 K2 时输出张量 `out` 完全不被任何 kernel 写入（bench 里它是 `torch.zeros`，输出保持全零）。

**④ `TMA_DISABLE_ALL` 的官方注释**：

[fwd_kernel2.cuh:3-5](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L3-L5)：注释明确说明「定义后彻底禁用 load/store warp，让 MMA warp 在没有流水线同步的情况下工作」。它裁剪的面非常广：

- [fwd_kernel2.cuh:172-181](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L172-L181)：`kTmaTransactionBytes` 的全部字节项被 `#ifndef` 包住，退化为 `0u`；
- [fwd_kernel2.cuh:199-213](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L199-L213)：两条流水线对象不再构造；
- [fwd_kernel2.cuh:240-318](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L240-L318)：初始状态的三条输入路径（bf16 直通 / fp32 转换 / 零初始化）整体消失；
- [fwd_kernel2.cuh:320-423](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L320-L423)：LOAD warp 的 8 份 TMA 拷贝循环消失；
- [fwd_kernel2.cuh:435-443](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L435-L443)：MMA 的 `consumer_wait/producer_acquire` 退化成 `constexpr int load_stage = 0;`——MMA warp 在**未初始化的 smem** 上跑完每 tile 52 次 gemm 的计算骨架；
- [fwd_kernel2.cuh:745-838](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L745-L838)：STORE warp、bf16/fp32 状态输出路径整体消失。

注意 K1 **不受** `TMA_DISABLE_ALL` 影响（K1 的开关是 `BLOCK_LEVEL_K1`，它的 TMA 加载/存储没有任何 `#ifndef TMA_DISABLE_ALL` 守卫）。

#### 4.1.4 代码实践：给 setup.py 加宏注入开关

setup.py 目前不暴露任何「额外 nvcc 宏」的入口，但已有一个现成的布尔环境变量解析函数 [setup.py:10-11](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L10-L11)（`is_flag_set`）。下面的补丁（**示例代码**，读者在自己分支上的临时修改，本讲义不改动源码）复用它：

```python
# setup.py 中新增（示例代码）
def get_ablation_flags():
    flags = []
    for name in ("BLOCK_LEVEL_K1", "BLOCK_LEVEL_K2"):
        v = os.getenv(f"FLASH_KDA_{name}")          # 传 -1 表示关闭
        if v is not None:
            flags.append(f"-D{name}={v}")
    if is_flag_set("FLASH_KDA_TMA_DISABLE_ALL"):
        flags.append("-DTMA_DISABLE_ALL")
    return flags
```

再在 `extra_compile_args` 的 `'nvcc'` 列表（[setup.py:70-83](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L70-L83)）末尾加一行 `*get_ablation_flags(),`。

之后即可用环境变量矩阵驱动四版本编译：

```bash
# (a) 基线
pip install -e . --no-build-isolation 2>&1 | tee build_a.log
# (b) 关闭 K1
FLASH_KDA_BLOCK_LEVEL_K1=-1 pip install -e . --no-build-isolation 2>&1 | tee build_b.log
# (c) 关闭 K2
FLASH_KDA_BLOCK_LEVEL_K2=-1 pip install -e . --no-build-isolation 2>&1 | tee build_c.log
# (d) 关闭 K2 的 TMA 流水线
FLASH_KDA_TMA_DISABLE_ALL=1 pip install -e . --no-build-isolation 2>&1 | tee build_d.log
```

每次重编前先 `touch csrc/smxx/fwd_launch.cu` 强制重建唯一翻译单元。完整的四版本计时与对照表见第 5 节综合实践；编译日志里 `--ptxas-options=-v`（[setup.py:79](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L79)）会顺手打印每个 kernel 的 smem/寄存器用量，正好留给 4.2 的实践用。若构建未触发重编或产物行为与预期不符，具体现象**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `FLASH_KDA_BLOCK_LEVEL_K1=0` 关不掉 Kernel 1？
**答案**：启动块的条件是 `#if BLOCK_LEVEL_K1 >= 0`（fwd_launch.cu:147），`0 >= 0` 为真，kernel 照常编译启动。必须传 `-1` 等负值。

**练习 2**：为什么命令行 `-DBLOCK_LEVEL_K1=-1` 能覆盖源码里的默认值 1？
**答案**：utils.cuh 用 `#ifndef BLOCK_LEVEL_K1` 守卫默认定义；命令行宏在预处理开始前就已定义，守卫不生效，默认值被跳过。

**练习 3**：定义了 `TMA_DISABLE_ALL` 之后，varlen 模式的 `_flash_kda_build_tile_prefix` 还会执行吗？
**答案**：会。它在 `#if BLOCK_LEVEL_K1 >= 0` 块内（fwd_launch.cu:164-167），只受 `BLOCK_LEVEL_K1` 控制，与 `TMA_DISABLE_ALL` 无关。

---

### 4.2 调参旋钮盘点

#### 4.2.1 概念说明

「旋钮」指改动代价小、值得做扫参实验的参数。本项目的旋钮分四类：**流水线深度**（smem 换延迟隐藏）、**并行结构**（线程数、warp 分工）、**寄存器/占用**（launch bounds、PREFETCH）、**构建旗标**（fast_math、寄存器分配力度）。盘点旋钮的关键是知道**每个旋钮的约束边界**——超过边界要么编译失败（static_assert）、要么静默破坏 bit-exact。

#### 4.2.2 核心流程：旋钮登记表

| 旋钮 | 位置 | 默认 | 约束与风险 |
| --- | --- | --- | --- |
| `kInputStages` / `kOutputStages` | [fwd_launch.cu:29-30](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L29-L30) | 3 / 2 | smem 上限约 227KB；**union 钳制效应**：降到 (2,2) 不省内存（见下） |
| `CHUNK` | [fwd_launch.cu:31](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L31) | 16 | 与 `lower_bound` 的指数域耦合（4.3.1）；`static_assert(CHUNK % 8 == 0)`（[fwd_kernel1.cuh:362-363](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L362-L363)）；求逆的 8x8×2 结构假设 16；beta smem 窗口 32 限制 CHUNK ≤ 32 |
| `kK1Threads` | [fwd_launch.cu:149](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L149) | 256 | 与 K1 的 L2 归一化映射、门控 128 线程分支、decay_apply 8-warp 分块**硬绑定**，不可单独调 |
| `kK2Threads` | [fwd_launch.cu:186](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L186) | 192 | `32*2+128`：4 MMA + 1 LOAD + 1 STORE 的 warp 专用化结构，改它等于重写 K2 骨架 |
| K1 launch bounds | [fwd_kernel1.cuh:120](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L120) | `(NumThreads, 8)` | 第二参数是每 SM 最少驻留 CTA 数：调高挤寄存器，调低降 occupancy |
| K2 launch bounds | [fwd_kernel2.cuh:133](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L133) | `(NumThreads)` | 未指定最小 CTA 数，本身就是一个可实验点 |
| `PREFETCH` | [fwd_kernel2.cuh:466](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L466) | 1 | Phase 6 预取环深度；1→2 每线程多占约 14 个寄存器（u3-l5 分析），可能触发 spill |
| `ELEMS_PER_THREAD` | [fwd_kernel1.cuh:268](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L268) | 8 | 必须整除 D 且 `D/8 × CHUNK == 256` 才覆盖满矩阵 |
| rsqrt ε | [fwd_kernel1.cuh:295-296](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L295-L296) | `1e-6f` | 数值旋钮：改动即破坏与 torch_ref 的 bit-exact |
| `--use_fast_math` | [setup.py:78](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L78) | 开 | 与 bit-exact 设计互为因果（u3-l8）：摘掉后 kernel 与参考实现需同步重对齐 |
| `--register-usage-level=10` / `-v` | [setup.py:79](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L79) | 10 / 开 | 调寄存器分配力度；`-v` 让每次编译打印 smem/寄存器统计 |
| `NVCC_THREADS` | [setup.py:14-16](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L14-L16) | 32 | 只影响编译速度 |
| `FLASH_KDA_CUDA_ARCHS` | [setup.py:35-52](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L35-L52) | auto | 构建期目标架构（u1-l3 已详述） |

#### 4.2.3 源码精读：smem 账本与 union 钳制

为什么 `(2,2)` 的流水线深度不省共享内存？手算 K2 的 smem 账本（D=128、CHUNK=16）：

- `state_acc`（常驻）：\(128 \times 128 \times 2\,\text{B} = 32\,\text{KB} \)
- 每个 `InputStorage`（[fwd_kernel2.cuh:84-93](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L84-L93)）：v 4096 + beta 64 + kd/qd/kr 3×4096 + g_total 512 + INV 512 + Mqk 512 = **17984 B**
- 每个 `OutputStorage`：4096 B
- `state_fp32_buf`（fp32 状态转换缓冲）：\(128 \times 128 \times 4\,\text{B} = 64\,\text{KB} \)

关键在 [fwd_kernel2.cuh:99-107](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L99-L107) 的匿名 union：流水线缓冲与 fp32 转换缓冲**共享同一块内存**（生命周期错开：转换只发生在主循环前后）。于是：

\[ \text{union 大小} = \max\big(\underbrace{S_{in} \times 17984 + S_{out} \times 4096}_{\text{流水线缓冲}},\ \underbrace{65536}_{\text{fp32 转换}}\big) \]

代入默认 (3,2)：\(3 \times 17984 + 2 \times 4096 = 62144 < 65536\)，被钳到 64KB；改成 (2,2)：\(2 \times 17984 + 2 \times 4096 = 44160\)，仍被钳到 64KB——**总 smem 约为 32 + 64 + 少量 barrier ≈ 98KB，一分未省**。要突破钳制必须升到 (4,2) 以上（\(4 \times 17984 + 8192 = 80128 > 65536\)），代价是总 smem 涨到约 113KB、occupancy 下降。这就是 u3-l2 所说「减 stage 数未必省内存」的算术根源。K1 侧同理有 Phase A/Phase B 的 union 复用（[fwd_kernel1.cuh:54-78](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L54-L78)），配合 `__launch_bounds__(256, 8)` 达成每 SM 8 CTA。

#### 4.2.4 代码实践：核对基线的 smem / 寄存器数字

1. **实践目标**：不改任何源码，用编译日志验证 4.2.3 的手算账本，为后续调参实验建立「基线刻度」。
2. **操作步骤**：
   ```bash
   touch csrc/smxx/fwd_launch.cu
   pip install -e . --no-build-isolation 2>&1 | tee build_baseline.log
   grep -A2 "_flash_kda_fwd" build_baseline.log
   ```
   `--ptxas-options=-v` 已内置于 [setup.py:79](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L79)，ptxas 会对每个编译出的 kernel 打印 `Used N registers`、`smem` 等行（K1 有 2 份实例化——varlen 与 batched；K2 有 14 份）。
3. **需要观察的现象**：`_flash_kda_fwd_prepare` 的 smem 约 21KB 量级（Phase A/B union 后）；`_flash_kda_fwd_recurrence` 的 smem 约 98KB 量级、寄存器不超过 64 左右。
4. **预期结果**：记录到的数字与 4.2.3 手算一致（允许少量 barrier/对齐开销）。具体数值**待本地验证**。
5. 把结果填进 4.2.2 的登记表旁边，作为你自己环境的基线列。

#### 4.2.5 小练习与答案

**练习 1**：把 `kInputStages/kOutputStages` 从 (3,2) 改成 (2,2)，smem 会省多少？
**答案**：省 0。流水线缓冲 44160B 仍小于 union 中 fp32 转换缓冲的 65536B 下界，总 smem 被 64KB 钳住不变；只有升到 (4,2) 以上才会真正增长（也就无所谓「省」）。

**练习 2**：把 `PREFETCH` 从 1 改成 2，最直接的代价是什么？怎么观测？
**答案**：Phase 6 的三组环形缓冲（A 片段、状态片段、衰减标量）翻倍，每线程静态多占约 14 个寄存器，可能触发寄存器 spill。观测手段：重编后看 ptxas `-v` 的寄存器数与 `--warn-on-spills` 警告（均已内置于 setup.py）。

**练习 3**：为什么 `--use_fast_math` 不能当作「可关的安全开关」？
**答案**：fast_math 下 `expf` 与 kernel 的 `ex2.approx.ftz` 数值同源，这是 kernel 与 torch_ref 达成 torch.equal 的契约一半（u3-l8/u3-l9）；摘掉后 host/设备两侧的指数实现不再逐位一致，bit-exact 测试会失败，需要同步重写参考实现。

---

### 4.3 head_dim 扩展影响面分析

#### 4.3.1 概念说明

`D`（head_dim）名义上是模板参数（`launch_fwd<int D, ...>`、`K1Layouts<D, CHUNK>`），但 **D=128 被三层硬编码锁死**：host 校验层、K1 的线程-数据映射层、K2 的 warp 分工层。评估「支持 D=64」的真实工作量 = 把这三层连同数值约束、参考实现、测试基准全部找出来。

另一条与 D 无关但同样架构级的约束是 **CHUNK 与 lower_bound 的耦合**（承接 u3-l8）：门控激活后的每步 gate 落在 \((\text{lower\_bound} \times \log_2 e,\ 0)\)（log2 域），CHUNK 步 inclusive cumsum 的下界为：

\[ \text{CHUNK} \times |\text{lower\_bound}| \times \log_2 e \;>\; -126 \]

一旦越过 fp32/bf16 的正常数指数下界 \(2^{-126}\)，`ex2.approx.ftz` 会把衰减系数冲零，衰减族四个变体的恒等式被破坏。代入 lower_bound = -5：16 步 ≈ -115.4（安全）、17 步 ≈ -122.6（贴边）、18 步 ≈ -129.8（越界）——所以 **CHUNK 上限是 17，而 16 是唯一同时满足 `% 8 == 0`、8x8×2 求逆结构与 beta 窗口的取值**。换更低的 lower_bound 或换门控激活时，这个不等式必须重新验证。

#### 4.3.2 核心流程：改 D=64 需要触碰的清单

自顶向下七层（✅ 表示模板自动适配，🔧 表示必须手工改）：

1. 🔧 **host 校验层**：`TORCH_CHECK(D == 128)` 直接拒绝；`get_workspace_size` 内部 `constexpr int D = 128` 独立硬编码；`LAUNCH` 宏里的 `launch_fwd<128, ...>` 字面量——D 目前根本不是运行时分支，分发宏要新增一层按 D 的路由。
2. 🔧 **显式实例化表**：14 份 `INSTANTIATE_LAUNCH_FWD(128, ...)` 要为 64 再来一遍。
3. ✅ **布局层**：`K1Layouts<64,16>`/`K2Layouts<64,16>` 的 `tile_to_shape` 自动适配；`WorkspaceSizes` 的 static_assert（`D * 4 % 128 == 0`）对 64 成立（256 ≡ 0 mod 128）；beta 的 32 元素窗口与 D 无关。
4. 🔧 **K1 线程映射层（真正的坑）**：L2 归一化假设「每行 D/8=16 线程 × CHUNK=16 行 = 256 线程」；门控/尾清零分支假设「前 128 线程列号 = D」——D=64 时都会越界（详见 4.3.3）。
5. 🔧 **K2 warp 分工层**：每 warp 恰好两个 16×16 列块的假设写死在寄存器数组长度与索引算式里；D=64 时每 warp 只剩一个列块。
6. ✅ **K 维循环**：`K_BLOCKS = D/16`、`S_M_BLOCKS = D/16` 是从布局 shape 推导的，自动变 4。
7. 🔧 **数值与生态层**：4.3.1 的范围论证需按新形状复核；`torch_ref.py` 的 `CHUNK`/布局假设、`test_fwd_full.py` 的形状表、FLA 后端契约（K=V=128）都要跟着动。

#### 4.3.3 源码精读

**① host 层的三处硬编码**：

[flash_kda.cpp:110](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L110)：`TORCH_CHECK(D == 128, "currently only supports D == 128")`——扩展的第一颗螺栓。[flash_kda.cpp:10-11](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L10-L11) 里 `get_workspace_size` 自带一份 `constexpr int CHUNK = 16; constexpr int D = 128;`，与 kernel 侧的模板参数**不共享**，改 CHUNK/D 时这里极易漏改（workspace 会按错误尺寸分配）。[flash_kda.cpp:185](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L185) 的 `LAUNCH` 宏把 `128` 写进 `launch_fwd<128, HI, HO, FP32, VL>`。加上 [fwd_launch.cu:228-238](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L228-L238) 的 14 行字面量实例化表，host 侧共四处。

**② K1 的 L2 归一化映射**：

[fwd_kernel1.cuh:268-271](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L268-L271)：`ELEMS_PER_THREAD = 8; THREADS_PER_ROW = D / 8; my_row = threadIdx.x / THREADS_PER_ROW`。D=128 时 16 线程/行 × 16 行恰好 256；D=64 时 8 线程/行，`my_row` 最大到 31，而 smem 矩阵只有 CHUNK=16 行——**第 16 行以后全部越界读写**。此映射必须重写（例如让 256 线程分两批处理行）。

**③ K1 的门控/尾清零双分支**：

[fwd_kernel1.cuh:311-314](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L311-L314)：`if (compute_tid < 128) { int col = compute_tid; ... dt_bias.begin()[col] ... }`——列号范围直接等于 128。D=64 时线程 64-127 会读过 `dt_bias`（只有 D 个元素）与 `g_bf16_smem[row * D + col]` 的末尾。[fwd_kernel1.cuh:331-337](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L331-L337) 的尾行清零分支（线程 128-255，`col = compute_tid - 128`）同样以 D=128 为前提。

**④ K2 的「每 warp 两个列块」假设**：

[fwd_kernel2.cuh:461-462](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L461-L462) 的注释写明分工：`Each warp handles TWO 16x16 column blocks (N=128 / 4 warps = 32 = 2 x 16)`。这个「2」渗透到各相位：寄存器数组 `u_acc[2], out_acc[2]`（[fwd_kernel2.cuh:527](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L527)）、v 加载与 out 写回的 `warp_id * 2 + i`（[fwd_kernel2.cuh:579](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L579)、[655](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L655)）、Phase 6 状态更新的 `ring_S_acc[2][PREFETCH]` 与 `warp_id * 2 + bi`（[fwd_kernel2.cuh:680](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L680)、[723](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L723)），还有 beta0/beta1 双标量。D=64 时列块总数是 4、每 warp 1 个，上述 `[2]` 全部塌缩成 `[1]`——不是改常量，而是删代码路径。对比之下 K 维的 [fwd_kernel2.cuh:534](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L534) `K_BLOCKS = size<1>(k_decayed) / 16` 从布局推导、自动适配——**从 shape 推导的都自适应，写死在数组长度里的都要手工改**。

**⑤ workspace 契约的连带**：`WorkspaceSizes`（[utils.cuh:64-77](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L64-L77)）对 D=64 满足全部对齐断言，但每 tile 字节数从 13824 变为 \(3 \times 16 \times 64 \times 2 + 64 \times 4 + 2 \times 512 = 8064\)，K1 的 TMA store 描述符与 K2 的 `kTmaTransactionBytes`（[fwd_kernel2.cuh:173-179](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L173-L179)）自动跟随 cosize 变化，Python 侧 `get_workspace_size` 则必须与 ① 同步手改。

**工作量结论**：这不是「改一个模板参数」而是「K1 三个线程映射 + K2 六个相位的 warp 分工重构 + host 四处 + 参考/测试/基准全链」。一个合理的估计是：布局与 TMA 层约 20% 工作量，K1 映射与 K2 相位重构约 60%，参考实现与 exact-match 恢复约 20%。

#### 4.3.4 代码实践：生成你自己的影响面清单（源码阅读型）

1. **实践目标**：不改代码，用检索把「D=128 / CHUNK=16 的硬编码假设点」全部定位，产出扩展 checklist。
2. **操作步骤**：
   ```bash
   # host 层字面量
   grep -n "launch_fwd<128" csrc/flash_kda.cpp
   grep -n "INSTANTIATE_LAUNCH_FWD(128" csrc/smxx/fwd_launch.cu
   grep -n "D == 128" csrc/flash_kda.cpp
   # K1 线程映射假设（256 线程 / 128 列 / 16 行）
   grep -n "THREADS_PER_ROW\|compute_tid < 128\|compute_tid - 128" csrc/smxx/fwd_kernel1.cuh
   # K2 每 warp 两列块假设
   grep -n "warp_id \* 2\|acc\[2\]\|\[2\]\[PREFETCH\]" csrc/smxx/fwd_kernel2.cuh
   # CHUNK 硬编码
   grep -rn "CHUNK = 16" csrc/ tests/torch_ref.py
   ```
3. **需要观察的现象**：每一处命中都对应 4.3.2 清单的一个条目；特别留意 `grep "CHUNK = 16"` 会命中**三个互不共享的定义**（flash_kda.cpp 两处 + torch_ref.py 一处，加上 fwd_launch.cu 的 `constexpr int CHUNK = 16` 共四处）。
4. **预期结果**：把命中整理成「文件:行号 → 假设内容 → D=64 时的行为（越界/需改/自动适配）」三列表格。纯静态分析，无需 GPU，可直接完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `K_BLOCKS` 能对 D 自适应，而 warp 列块数不能？
**答案**：`K_BLOCKS` 由 `size<1>(k_decayed) / 16` 从布局 shape 推导（fwd_kernel2.cuh:534），是运行时无关的编译期算式；列块数则决定了寄存器数组的声明长度（`u_acc[2]` 等）与每 warp 的固定分工，写死在类型和索引算式里，shape 推导救不了它。

**练习 2**：D=64 时 K2 的 union 钳制线变成多少？对调参意味着什么？
**答案**：`state_fp32_buf` 变为 \(64 \times 64 \times 4 = 16\,\text{KB}\)，钳制线大幅下降；此时 `kInputStages` 升到 4 甚至更高都可能仍被 16KB 钳住（每 stage input 约 8064+4096+64 ≈ 12KB，两个 stage 即超线），流水线深度与 smem 的关系需要按新账本重算——旋钮的约束边界随 D 移动。

**练习 3**：如果把门控激活从 sigmoid 换成别的（例如硬裁剪），影响面清单要加哪些条目？
**答案**：至少五处——utils.cuh 的 `sigmoid_tanh_approx_f32` 及其两个调用点（K1 门控 [fwd_kernel1.cuh:323](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L323)、K2 beta 激活 [fwd_kernel2.cuh:586-587](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L586-L587)）、host 侧 `gate_scale = lower_bound * log2(e)` 的换底（[flash_kda.cpp:128](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L128)）、4.3.1 的指数域范围不等式（新激活的值域必须重新代入）、以及 torch_ref 的同步复刻，否则 exact-match 立即断裂。

---

## 5. 综合实践

**任务：完成一次完整的四版本结构消融，输出对照表并解释每个流水线阶段的贡献。**

前置：一台 SM90 机器、按 u1-l3 完成基线安装、按 4.1.4 给 setup.py 打好宏注入补丁（记得先 `git checkout -b ablation` 开实验分支）。

**步骤**：

1. **基线标定**：空环境变量重编，确认 `python tests/test_fwd.py` 全部通过（含 bit-exact 对拍与 FLA 对拍），记录基线 bench：
   ```bash
   python benchmarks/bench_fwd.py --mode fixed --warmup 30 --iters 100 --repeats 5
   ```
   记录 `flash_kda (bf16 state)` 一行的 mean/min/max（bench 的 CLI 与输出格式见 [benchmarks/bench_fwd.py:145-163](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L145-L163)）。

2. **四版本循环**：对 4.1.4 的 (a)(b)(c)(d) 四种环境变量组合，各执行：
   ```bash
   touch csrc/smxx/fwd_launch.cu
   FLASH_KDA_BLOCK_LEVEL_XX=... pip install -e . --no-build-isolation 2>&1 | tee build_X.log
   python tests/test_fwd.py 2>&1 | tail -5 ; echo "exit=$?"   # 记录通过/失败
   python benchmarks/bench_fwd.py --mode fixed --iters 100     # 记录耗时
   ```
   注意 `.so` 是覆盖式安装，同一时刻只有一个版本在环境里——**顺序执行，做完一个再编下一个**。

3. **汇总成消融对照表**（模板）：

   | 版本 | 宏设置 | 测试结果 | `out` 是否被写 | fixed 耗时 (ms) | 相对基线 |
   | --- | --- | --- | --- | --- | --- |
   | (a) 基线 | 无 | 全部通过 | ✅ | \(t_a\) | 1.00 |
   | (b) 去 K1 | `BLOCK_LEVEL_K1=-1` | 预期失败（输出=垃圾） | ✅（错误值） | \(t_b\) | — |
   | (c) 去 K2 | `BLOCK_LEVEL_K2=-1` | 预期失败（out 保持零） | ❌ | \(t_c\) | — |
   | (d) 去 K2 流水线 | `TMA_DISABLE_ALL` | 预期失败 | ❌ | \(t_d\) | — |

   测试失败是**预期结果**而非事故：消融版本本来就是计时骨架。用 `|| true` 包住测试命令以便脚本继续，记录的是「哪个断言以多大误差失败」。

4. **归因分析**（两个可检验的等式）：
   - K1 与 K2 在同一 stream 上串行，因此 \(t_a \approx t_b + t_c\)（差值为启动开销与 occupancy 波动），由此得到两 kernel 的耗时占比；
   - K2 中「load/store warp + 双流水线」的贡献 ≈ \(t_c - t_d\)，\(t_d\) 是 MMA 计算骨架的下界。
   结合 u3-l4/u3-l5 的静态分析（每 warp 每 tile 52 次 gemm、Phase 1 占 32 次）解释数字：如果 \(t_c - t_d\) 占比大，说明瓶颈在访存流水线；如果 \(t_d\) 本身就接近 \(t_a\)，说明 K2 是计算主导。

5. **收尾**：`git checkout setup.py && git checkout master`，空环境变量重装回基线，重跑一次测试确认环境干净。

具体毫秒数依赖硬件，**待本地验证**；本实践交付的是流程、表格与归因等式，而不是固定数字。

---

## 6. 本讲小结

- 三个结构消融开关（`BLOCK_LEVEL_K1/K2`、`TMA_DISABLE_ALL`）全部是编译期裁剪：关闭 kernel 级开关要传**负值**（`#if >= 0` 语义，传 0 无效），`TMA_DISABLE_ALL` 只作用于 K2 的访存流水线、不碰 K1。
- 消融开关是计时骨架而非功能回退：任何非基线组合都会让 exact-match 测试失败，消融实验的价值在于耗时归因（\(t_a \approx t_b + t_c\)，\(t_c - t_d\) = 流水线贡献）。
- 调参旋钮的边界比旋钮本身更重要：K2 的 union 钳制使 (3,2)→(2,2) 不省 smem；`--use_fast_math` 与 bit-exact 互为因果；`PREFETCH`/launch bounds 动的是寄存器账本。
- 「支持 D=64」是全链改造：host 四处字面量 + 实例化表、K1 三个硬编码线程映射（L2 归一化、门控 128 线程、尾清零）、K2 六个相位的「每 warp 两列块」假设、workspace 账本与 torch_ref 同步——从 shape 推导的量自动适配，写死在数组长度与索引里的都要手工改。
- CHUNK 与 lower_bound 通过指数域不等式 \(\text{CHUNK} \times |\text{lower\_bound}| \times \log_2 e > -126\) 耦合，lower_bound=-5 下 CHUNK 上限 17，这使得 16 几乎是唯一可行取值。
- 二次开发的标准流程：开分支 → 改一处（或注入一个 `-D`）→ `touch` + `pip install -e . --no-build-isolation` → `python tests/test_fwd.py` 回归 → `benchmarks/bench_fwd.py` 计时 → 记录进旋钮登记表。

## 7. 下一步学习建议

至此整套手册的 12 讲已经闭环：从 KDA 递推数学读到 warp 专用化流水线，再到本讲的改造实践。继续深入的方向：

1. **做一次真实的调参扫参**：在 4.2 登记表的基础上，把 `kInputStages ∈ {2,3,4,5}` × `PREFETCH ∈ {1,2}` 在你的目标形状上扫一遍，用 `benchmarks/ncu.sh` 的 occupancy/smem 指标解释结果，写成你自己的 BENCHMARK 笔记。
2. **挑战一个小型扩展**：以 4.3 的清单为路线图，尝试给 host 层加一个 `TORCH_CHECK(D == 64)` 的旁路并只跑通「布局编译 + 实例化 + workspace 对账」三步（不追求 K1/K2 计算正确），体会全链工作量。
3. **回读上游**：带着本讲的裁剪视角重读 CUTLASS 的 `cutlass/pipeline/sm90_pipeline.hpp` 与 `cute/atom/copy_traits_sm90_tma.hpp`，理解 FlashKDA 依赖的流水线与 TMA 原语本身提供了哪些本项目没用到的旋钮（如 cluster 多播）。
4. **关注上游演进**：`git log` 跟踪 MoonshotAI/FlashKDA 与 flash-linear-attention 的后续提交——本讲分析的 `lower_bound × CHUNK` 耦合与 D=128 锁定正是最可能被后续版本松绑的两处。

# 扩展实践：新增一个 group_size/num_bits 配置的完整链路

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出「新增一个量化配置」必须触碰的全部位置：`decode_api.cpp` 的两处 dispatch if 链、`genfile/` 下的显式实例化、必要时 `setup.py` 的源文件列表。
2. 理解模板显式实例化（explicit instantiation）与显式特化（explicit specialization）在两个 genfile 中的不同用法，以及「只解开一边」分别会发生什么（链接错误 / 死代码）。
3. 在动手改代码之前，先用 `kernel_traits.h` 的常量派生公式做编译期可行性检查，判断目标配置能否整除、共享内存是否放得下。
4. 独立完成综合实践：为 `group_size=64` 打通 2-bit k-channel 路径——从取消注释、重新编译到用 `evaluation/test.py` 验证正确性，并记录每一步的错误与解决方式。

本讲是第七单元的第三篇。前两篇（u7-l1、u7-l2）讲的是「怎么测」，本讲讲的是「怎么改」。

## 2. 前置知识

### 2.1 模板参数与运行时参数的分工（回顾）

在 u3-l1 已经建立过这个认知，这里只复习结论：

- `num_bits`、`quant_mode`、`group_size` 是 **编译期模板参数**。它们决定 kernel 内的循环边界、共享内存布局、MMA 形状——这些量在编译时就固化成了指令。
- Python 层传进来的 `num_bits=4`、`group_size=32` 等是 **运行时参数**。C++ 侧必须用一条 `if (params.group_size == ...)` 链，把运行时的值路由到「已经为这个值编译出来的那份模板实例」上。

所以「支持一个新配置」= 「让那份模板实例存在」+「让 if 链能找到它」。两者缺一不可。

### 2.2 显式实例化与显式特化：一段 30 秒的 C++ 课

CUDA 模板 kernel 的函数体写在头文件里（如 `flash_fwd_launch_template.h`），但**头文件里的模板定义本身不会生成任何机器码**——只有当某个编译单元真的用到了某个具体参数组合，编译器才会为那个组合生成代码。让代码「生成出来」有两种写法：

- **显式实例化**：`template void run_mha_fwd_splitkv_dispatch<half_t, 128, false, 1, 2, 64>(...);`
  意思是「请用这组参数把头文件里的那个模板生成一份」。前提是模板的**主定义**可见。
- **显式特化**：`template<> void run_kvcache_qpack_<half_t, 128, 1, 2, 64>(...) { ...另一段函数体... }`
  意思是「这组参数的行为由我这段专门写的函数体决定」。它不需要主模板有定义。

本项目的两个 genfile 恰好各用了一种——split kernel 用显式实例化，qpack kernel 用显式特化，原因见 4.2。

### 2.3 链接错误长什么样

`decode_api.cpp`（C++ 绑定层）只 include 了 `include/flash.h`，里面是**纯声明**。真正调用 `run_mha_fwd_splitkv_dispatch<..., 64>` 时，链接器需要找到这个符号的定义；如果 genfile 里没有对应的实例化，就会报：

```
undefined reference to `void run_mha_fwd_splitkv_dispatch<cutlass::half_t, 128, false, 1, 2, 64>(Flash_fwd_params&, cudaStream_t)'`
```

看到这种错误不要慌——它正是「dispatch 与实例化没有成对解开」的直接证据。

### 2.4 静默落空（silent no-op）

更危险的是反过来：**如果 dispatch 分支与实例化都没解开，代码照样编译、照样运行、不报任何错**。if 链落空后函数直接结束，一个 kernel 都不会启动：`k_pack` 保持调用方初始化时的全零，`out` 是 `torch::empty_like` 的未初始化内存。结果就是「程序不崩、输出全是垃圾」。这是本讲反复强调的验证动机。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `csrc/bit_decode/decode_api.cpp` | pybind11 绑定层 | `run_mha_fwd` / `run_kvcache_qpack` 两个 dispatch if 链：哪些分支启用、哪些被注释 |
| `csrc/bit_decode/src/include/flash.h` | 模板声明 | 三个 launch 函数的原型（decode_api.cpp 只看得到这些） |
| `csrc/bit_decode/src/flash_fwd_launch_template.h` | 模板定义 | `run_mha_fwd_splitkv_dispatch` 与 `run_kvcache_qpack_hdim128` 如何把模板参数灌进 traits |
| `csrc/bit_decode/src/genfile/*.cu`（5 个） | 实例化单元 | 每个量化配置对应一行（被）注释的实例化语句 |
| `csrc/bit_decode/src/include/kernel_traits.h` | 编译期图纸 | group_size 如何派生 `num_params`、`kBlockK_params` 等常量 |
| `csrc/bit_decode/src/include/qpack.h` | 量化原语 | `qpack_kc_vt<2>` 中与 num_params 相关的硬编码 |
| `csrc/bit_decode/src/flash_fwd_kernel.h` | kernel 本体 | `compute_qpack_1rowblock` 中参数落盘的索引数学 |
| `setup.py` | 构建 | 源文件列表与 include 路径；何时需要改、何时不需要 |
| `bit_decode/bit_decode_interface.py` | Python 门面 | `num_bits` 分流；新增 num_bits 时这里也要动 |
| `evaluation/test.py` | 验证工具 | 修改配置常量即可复用为新配置的正确性测试 |

## 4. 核心概念与源码讲解

### 4.1 三层配对机制：dispatch、实例化与构建源列表

#### 4.1.1 概念说明

一个量化配置（quant_mode × num_bits × group_size）要在 BitDecoding 里跑起来，必须同时满足三层条件，我们称之为「三层配对」：

1. **dispatch 层**（`decode_api.cpp`）：运行时的 `if (params.group_size == ...)` 链里有这个分支，且分支体没有被注释。
2. **实例化层**（`genfile/*.cu`）：这组模板参数被显式实例化/特化，机器码真的存在。
3. **构建层**（`setup.py`）：承载该实例化的 `.cu` 文件出现在 `sources` 列表里（以及 include 路径里有 cutlass）。

任何一层缺失，表现各不相同：缺第 1 层 → 静默落空（见 2.4）；缺第 2 层 → 链接错误（见 2.3）；缺第 3 层 → 整个文件里的所有实例化都不存在，同样表现为链接错误。**注意第三层的单位是「文件」而不是「配置」**——给已有文件添加一行实例化不需要动 setup.py；只有新建 `.cu` 文件才需要。

#### 4.1.2 核心流程

以 decode 阶段为例，一次配置从 Python 到 GPU 的完整路由：

```text
Python: fwd_kvcache_int(..., quant_mode="k-channel", group_size=64, num_bits=2)
  │  按 num_bits 分流到绑定函数 fwd_kvcache_int2          (bit_decode_interface.py)
  ▼
C++: mha_fwd_kvcache<2>(...)                              (decode_api.cpp:316)
  │  set_params_fprop 把 group_size=64 写进 params        (decode_api.cpp:57-58)
  │  run_mha_fwd<2>(params, stream, force_split=true)     (decode_api.cpp:519)
  ▼
dispatch: if (quant_mode == "k-channel")
  │    ├─ group_size == 128 → 实例 (1, 2, 128)   [已启用]
  │    ├─ group_size == 64  → 实例 (1, 2, 64)    [当前被注释 → 落空!]
  │    └─ group_size == 32  → 实例 (1, 2, 32)    [已启用]
  ▼
genfile: flash_fwd_split_hdim128_fp16_sm80_2bit.cu 里对应的那行实例化
  ▼
kernel: Flash_fwd_kernel_traits<128, 16, 256, 4, false, false, 1, 2, 64> 的常量
```

#### 4.1.3 源码精读

**dispatch 第一处：`run_mha_fwd`（decode 注意力路径）。** 整个函数就是一张「启停表」：

- [decode_api.cpp:199-206](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L199-L206)：k-channel 分支。`group_size == 128`（L200-201）与 `group_size == 32`（L204-205）的调用是活的；**L202-203 的 `group_size == 64` 调用整行被注释**——这就是本讲实践要解开的位置之一。
- [decode_api.cpp:207-215](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L207-L215)：else 分支即 k-tensor（quant_mode=0）模式，三个 group_size 的调用**全部被注释**——k-tensor 解码路径整体未启用（u4-l3 讲过其量化器 `quant_Ktensor` 也另有硬编码，本讲不去碰它）。
- 注意 if 链没有任何 `else` 兜底报错：所有分支都不命中时函数静默返回，这正是 2.4 描述的行为根源。

**dispatch 第二处：`run_kvcache_qpack`（prefill 量化打包路径）。** 结构完全同构：

- [decode_api.cpp:221-228](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L221-L228)：k-channel 分支，`group_size == 32`（L222-223）与 `== 128`（L226-227）启用，**L224-225 的 `== 64` 被注释**——实践的第二个修改点。
- [decode_api.cpp:229-237](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L229-L237)：k-tensor 分支全注释。

两个函数的调用点分别在 [decode_api.cpp:519](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L519)（decode 恒以 `force_split_kernel=true` 走 split 路径）与 [decode_api.cpp:679-682](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L679-L682)（qpack 仅当 `max_seqlen_k > 0` 才启动）。**修改必须覆盖两处 dispatch**：只解开 decode 侧，prefill 的打包会静默落空，`k_pack` 全零，decode 输出自然错误；只解开 qpack 侧，prefill 正常但每步 decode 落空。

**构建层：`setup.py` 的源列表。**

- [setup.py:126-136](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/setup.py#L126-L136)：`CUDAExtension(name="bit_decode_cuda", sources=[...])` 列出 `decode_api.cpp` 加 5 个 genfile `.cu`。**本实践给 2-bit 的两个已有文件各加一行实例化，setup.py 一个字都不用改**；只有当你新建 `flash_fwd_split_hdim128_fp16_sm80_3bit.cu` 这类文件时才需要往这里追加。
- [setup.py:159-163](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/setup.py#L159-L163)：include 路径包含 `libs/cutlass/include`——新文件同样自动继承，无需额外配置。

**Python 门面的边界：** [bit_decode_interface.py:26-45](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L26-L45) 按 `num_bits` 分流到 `kvcache_pack_int4/int2`，其余值在 L44-45 直接 `raise ValueError`。**改 group_size 不需要动 Python 层**（group_size 是普通运行时参数一路透传）；只有新增 num_bits（如 3-bit）才要在这里加分支并在 pybind 侧导出新函数——那是完全不同量级的工程（LOP3 反量化器也要新写特化，见 u5-l3）。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「静默落空」不是理论——在**不重新编译**的前提下向现有构建传入未启用的 group_size=64，观察它既不报错也不干活。

**操作步骤**（需要已按 u1-l2 安装的 GPU 环境；若无 GPU 则跳到观察方式的替代方案）：

1. 写一个最小脚本（示例代码，非项目原有文件）：

   ```python
   # probe_g64.py —— 探测 group_size=64 是否被 dispatch 命中（示例代码）
   import torch
   from bit_decode import kvcache_pack_int, fwd_kvcache_int

   device, dtype = "cuda", torch.float16
   b, s, h, d = 1, 256, 2, 128          # s=256 恰为一个 2-bit 打包块
   g, nb = 64, 2
   pn = 16 // nb

   k = torch.randn(b, s, h, d, device=device, dtype=dtype)
   v = torch.randn(b, s, h, d, device=device, dtype=dtype)
   k_pack   = torch.zeros(b, s // pn, h, d, dtype=torch.uint16, device=device)
   k_params = torch.zeros(b, s // g,  h, d, dtype=torch.float32, device=device)
   v_pack   = torch.zeros(b, s, h, d // pn, dtype=torch.uint16, device=device)
   v_params = torch.zeros(b, d // g,  h, s, dtype=torch.float32, device=device)
   cu = torch.arange(0, (b + 1) * s, s, dtype=torch.int32, device=device)

   kvcache_pack_int(k, k_pack, k_params, v, v_pack, v_params,
                    None, cu, s, "k-channel", g, nb)
   print("k_pack 非零元素数 =", (k_pack != 0).sum().item())
   ```

2. 先把 `g` 改成 32 跑一遍，再改回 64 跑一遍，对比打印。

**需要观察的现象**：`g=32` 时 `k_pack` 几乎全非零（真实量化数据）；`g=64` 时 `k_pack` 非零元素数为 0——kernel 根本没启动，函数却正常返回。

**预期结果**：如上。若你的 `bit_decode_cuda` 是从包含 g=64 补丁的源码编译的，则两种 g 都非零。**待本地验证**（本讲义写作环境无 GPU）。

**无 GPU 替代方案（纯阅读，5 分钟）**：统计 [decode_api.cpp:199-215](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L199-L215) 与 [decode_api.cpp:221-237](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L221-L237) 两条 if 链：共 12 个分支（2 个函数 × 2 种 quant_mode × 3 种 group_size），其中**只有 4 个是活的**，全部集中在 k-channel × {32, 128}。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fwd_kvcache_int` 传了 `residual_block_size` 参数，kernel 却「不消费」它？
**答案**：残余块大小实际由编译期常量决定——`kernel_traits.h:75` 的 `residual_block_size = num_bits == 4 ? 128 : 256`，随实例化固化。运行时传的这个参数是哑参数（u3-l1 提过）。这再次说明：配置真正生效的通道是模板参数，不是 Python 参数。

**练习 2**：如果只解开 decode 侧（`run_mha_fwd`）的 g=64 分支并重新编译，程序会在哪个阶段失败？
**答案**：链接阶段。`decode_api.cpp` 调用 `run_mha_fwd_splitkv_dispatch<..., 1, 2, 64>`，但 2-bit split genfile 里没有该符号的定义（被注释），链接器报 undefined reference。运行阶段都到不了。

### 4.2 genfile 显式实例化精读：同一个机制的两种写法

#### 4.2.1 概念说明

`csrc/bit_decode/src/genfile/` 下有 5 个 `.cu` 文件（u1-l2 梳理过清单）。本讲只看其中 4 个量化相关的文件，它们是「配置清单的实体」：**dispatch if 链里的每一行，都能在这里找到一一对应的（启用或被注释的）一行**。

两个文件用了不同的 C++ 机制，值得分别理解：

- **split（decode 注意力）文件用显式实例化**。因为 `run_mha_fwd_splitkv_dispatch` 的主模板在 [flash_fwd_launch_template.h:130-137](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L130-L137) 有完整定义，genfile 只需一行 `template void ...<...具体参数...>(...);` 让编译器据此生成代码。
- **qpack（量化打包）文件用显式特化**。`run_kvcache_qpack_` 在 [flash.h:205](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L205) 只有声明、**没有主模板定义**；每个 genfile 里的 `template<>` 函数体就是该参数组合的唯一实现，内部再转调 [flash_fwd_launch_template.h:200-206](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L200-L206) 的 `run_kvcache_qpack_hdim128`。

为什么拆成多个文件？注释里写了答案：`// Splitting the different head dimensions to different files to speed up compilation.`——CUDA 模板编译极慢，拆文件让 nvcc 以 `--threads 4`（[setup.py:77-78](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/setup.py#L77-L78)）并行编译。这也意味着**每新增一个实例化都会增加所属文件的编译时间**，但远比新增文件再串行划算。

#### 4.2.2 核心流程

模板参数的位置约定（两种文件共享同一顺序，务必背下来）：

```text
split:   run_mha_fwd_splitkv_dispatch<T, Headdim, Is_causal, quant_mode, num_bits, group_size>
qpack:   run_kvcache_qpack_<T, Headdim, quant_mode, num_bits, group_size>
traits:  Flash_fwd_kernel_traits<kHeadDim, kBlockM, kBlockN, kNWarps, Is_Q_in_regs, Share_Q_K_smem, quant_mode, num_bits, group_size, elem_type>
```

对照关系：decode_api.cpp 里 `run_mha_fwd_splitkv_dispatch<cutlass::half_t, 128, false, 1, num_bits, 128>` 的 `1` 是 quant_mode（1=k-channel，0=k-tensor），`128` 是 group_size——与 Python 侧字符串 `"k-channel"` 的比较发生在 dispatch 的 if 条件里（`params.quant_mode == "k-channel"`），进入模板后又变回整数。

#### 4.2.3 源码精读

**2-bit split 文件（本实践主战场之一）：**

- [flash_fwd_split_hdim128_fp16_sm80_2bit.cu:7-9](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_fwd_split_hdim128_fp16_sm80_2bit.cu#L7-L9)：L7 是 `(..., 1, 2, 128)` 已启用；**L8 是 `(..., 1, 2, 64)` 被注释——解开它**；L9 是 `(..., 1, 2, 32)` 已启用。注意此文件没有任何 quant_mode=0 的行：2-bit 的 k-tensor 连被注释的预留都没有。
- 文件头 `#include "../flash_fwd_launch_template.h"`（L5）是显式实例化的前提——主模板定义必须可见。

**2-bit qpack 文件（另一主战场）：**

- [flash_qpack_hdim128_fp16_sm80_2bit.cu:7-18](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_qpack_hdim128_fp16_sm80_2bit.cu#L7-L18)：三段特化对应 g=128（L7-10 启用）、**g=64（L11-14 被注释——解开这 4 行）**、g=32（L15-18 启用）。每段是完整的 `template<> void run_kvcache_qpack_<...>(...) { run_kvcache_qpack_hdim128<...>(...); }` 函数体，注释必须整段解开，不能只解签名行。

**4-bit 两个文件（对照组，本实践不动它们）：**

- [flash_fwd_split_hdim128_fp16_sm80_4bit.cu:7-14](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_fwd_split_hdim128_fp16_sm80_4bit.cu#L7-L14)：L7-9 是 k-tensor（quant_mode=0）三行全注释；L12/14 是 k-channel g=128/32 启用，L13 是 g=64 注释。
- [flash_qpack_hdim128_fp16_sm80_4bit.cu:7-32](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_qpack_hdim128_fp16_sm80_4bit.cu#L7-L32)：g=32（L7-10）与 g=128（L15-18）启用；g=64（L11-14）与全部 k-tensor（L21-32）注释。**结构规律：4-bit 文件是 2-bit 文件的超集，dispatch 与实例化在「哪些行活着」上严格镜像。**

**从实例化到 kernel 的最后一跳：**

- [flash_fwd_launch_template.h:130-137](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L130-L137)：`run_mha_fwd_splitkv_dispatch` 把 `kBlockM=16`（L132）、`kBlockN=256`（L134）两个 dispatch 层写死的值与模板参数拼成 `Flash_fwd_kernel_traits<Headdim, 16, 256, 4, false, false, quant_mode, num_bits, group_size, T>`（L136）。**这就是 u5-l1 讲过的「仓库仅实例化 4 个 k-channel 配置」的机制源头**——block 尺寸不进模板参数表，所以同一份 traits 天然只对应一种 tile 形状。
- [flash_fwd_launch_template.h:200-206](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L200-L206)：qpack 侧同构，但 `kBlockN = num_bits == 4 ? 128 : 256`（L203）由 num_bits 推出——注意这与 fwd traits 的 `kBlockN_pack` 公式（kernel_traits.h:83）一致，两处必须同步，改一处不改另一处就会布局错乱。

#### 4.2.4 代码实践

**实践目标**：建立「注释掉实例化 → 链接错误」的肌肉记忆，学会读链接错误反推缺失的配置。

**操作步骤**：

1. 在**不改任何源码**的情况下，先做纸面推演：假设把 [decode_api.cpp:203](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L203) 解开（只这一处），写出你预期的链接错误符号名。
2. 若有编译环境（GPU 非必需，nvcc + torch 即可产生 `.o` 并触发链接），实际做一次这个单边实验：临时解开 L203，执行 `python setup.py build_ext --inplace`，捕获错误，再还原。

**需要观察的现象**：链接器报错的符号里应完整包含模板实参 `<cutlass::half_t, 128, false, 1, 2, 64>`——链接错误把「缺哪个配置」原样告诉你。

**预期结果**：`undefined reference to void run_mha_fwd_splitkv_dispatch<cutlass::half_t, 128, false, 1, 2, 64>(...)`。**待本地验证**。

**注意**：单边解开 qpack 侧（只解特化不解 dispatch）不会报错——那份特化只是没人调用的死代码，白付编译时间。这解释了为什么开发时要「成对解开、成对验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 split 文件用一行 `template void ...;` 而 qpack 文件要写四行函数体？
**答案**：split 的主模板在头文件里有定义，显式实例化一行即可让编译器按模板生成；qpack 的 `run_kvcache_qpack_` 只有声明没有主定义，必须为每组参数写显式特化的函数体（内部转调有定义的 `run_kvcache_qpack_hdim128`）。

**练习 2**：模板实参 `<cutlass::half_t, 128, false, 1, 2, 64>` 中每个值分别来自哪里？
**答案**：`half_t` 与 `128`（headdim）来自 launch 模板的硬编码（只支持 fp16/hdim128）；`false` 是 Is_causal（decode 单 token 恒非因果）；`1` 是 quant_mode=k-channel（来自 Python 字符串在 dispatch 处的比较结果）；`2` 是 num_bits（Python 层分流决定调用 `fwd_kvcache_int2`）；`64` 是 group_size（if 链选中）。

**练习 3**：如果想让 4-bit 也支持 g=64，需要在几个文件里改几处？
**答案**：4 个文件各 1 处，共 4 处：`decode_api.cpp:203` 一侧的 4-bit 版本其实在 L202-203 是与 num_bits 无关的模板（`run_mha_fwd<num_bits>` 内部展开），故 4-bit 只需解开 `decode_api.cpp` 里 g=64 分支一次即可同时覆盖 2/4-bit 的 decode；但 genfile 侧 2-bit 与 4-bit 是不同文件，需各自解开一行（split）与一段（qpack）。准确说：`decode_api.cpp` 两处 dispatch + `flash_fwd_split_..._4bit.cu` 一行 + `flash_qpack_..._4bit.cu` 一段。而 2-bit 的 genfile 修改与 4-bit 互不影响——这就是模板按 (num_bits, group_size) 二维展开的含义。

### 4.3 编译期可行性：group_size=64（2-bit）的常量推导

#### 4.3.1 概念说明

解开注释之前，必须先回答一个问题：**这组模板参数能通过 kernel_traits 的常量推导吗？** 所有派生常量都是整数运算（`kBlockN / group_size` 这类），如果除不尽，编译直接失败（或更糟——静默截断产生错误布局）。提前手算一遍，能把「编译失败」从挫折变成确认。

这一步同时也是对 u5-l1 traits 知识的应用性复习：不记得 `kBlockP` 是什么的读者请先回看 u5-l1 的常量派生链。

#### 4.3.2 核心流程

可行性判据清单（对 k-channel、2-bit、group_size=64，kBlockN=256 由 dispatch 写死）：

1. `group_size | kBlockN`（256）：块内整组，否则 `kBlockK_params` 非整数；
2. `group_size | kHeadDim`（128）：否则 `kHeadDim_v_params` 非整数；
3. `group_size | residual_block_size`（256）：残余块攒满时必须恰好装整数个组；
4. `pack_num | group_size`：一个 uint16 的所有槽位须同属一个量化组（u2-l1 的贯穿不变量）；
5. `num_params ≤ 8`：qpack 归约要求（见 4.4.3 对 reduce_tmp 的分析）。

推导表（公式列给出定义处的行号，便于核对）：

| 常量 | 公式（定义处） | g=32 已启用 | **g=64 目标** | g=128 已启用 |
| --- | --- | --- | --- | --- |
| `pack_num` | `16/num_bits`（traits:73） | 8 | 8 | 8 |
| `kBlockN` | dispatch 写死（launch:134） | 256 | 256 | 256 |
| `kBlockN_pack` / `residual_block_size` | traits:83 / :75 | 256 | 256 | 256 |
| `kBlockP` | `kBlockN/pack_num`（traits:85） | 32 | 32 | 32 |
| `kBlockK_params` | `kBlockN/group_size`（traits:87） | 8 | **4** | 2 |
| `kHeadDim_pack` | `128/pack_num`（traits:90） | 16 | 16 | 16 |
| `kHeadDim_v_params` | `128/group_size`（traits:93） | 4 | **2** | 1 |
| `tile_paramsk_g` | `(256/32)·(256/g)`（traits:103） | 64 | **32** | 16 |
| `num_params` | `kBlockN_pack/group_size`（traits:111） | 8 | **4** | 2 |

结论：**五条判据全部通过，所有常量为整数，g=64 编译期可行。**

#### 4.3.3 源码精读

- [kernel_traits.h:70-75](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L70-L75)：`quant_mode/group_size/num_bits/pack_num` 与 `residual_block_size` 的定义。注意 75 行的三元表达式只依赖 num_bits——`residual_block_size` **与 group_size 无关**，这是 g=64 不触碰残余块大小、判据 3 自动满足的保证。
- [kernel_traits.h:83-93](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L83-L93)：`kBlockN_pack/kBlockP/kBlockK_params/kHeadDim_pack/kHeadDim_k/kHeadDim_k_params/kHeadDim_v_params` 七个常量。k-channel（quant_mode==1）下 `kHeadDim_k = kHeadDim`、`kHeadDim_k_params = kHeadDim`——K 参数不缩维，只有 V 参数按 `128/group_size` 缩维。
- [kernel_traits.h:103-111](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L103-L111)：`tile_params*` 家族与 `num_params`。源码在这里留着两条 `// TODO: check` 注释——作者本人也标注了这些公式未经充分审查，这是 4.4 节要谨慎的信号。
- qpack 侧镜像定义：[kernel_traits.h:459-487](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L459-L487)（`Flash_qpack_traits`），公式相同，`num_params` 在 L487。**共享内存随 group_size 的变化量很小**：`SmemLayoutVParams` 从 256×4 变 256×2（half2，差 1KB 量级），总量仍在 u5-l1 给出的 144KiB 一带，`run_flash_splitkv_fwd` 里统一的 `cudaFuncSetAttribute` 抬限逻辑（[flash_fwd_launch_template.h:91-104](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L91-L104)）自动处理，无需干预。

#### 4.3.4 代码实践

**实践目标**：用一段不依赖 CUDA 的 C++ 程序验证手算表（任何装了 g++ 的机器都能做，5 分钟）。

**操作步骤**：

1. 把 traits 的常量公式抄进一个 `constexpr` 程序（示例代码，非项目原有文件）：

   ```cpp
   // traits_check.cpp —— 复刻 kernel_traits.h 的常量派生（示例代码）
   #include <cstdio>
   int main() {
       constexpr int kHeadDim = 128, kBlockN = 256;   // dispatch 写死
       for (int group_size : {32, 64, 128}) {
           constexpr int num_bits = 2;
           const int pack_num          = 16 / num_bits;
           const int residual_bs       = num_bits == 4 ? 128 : 256;
           const int kBlockP           = kBlockN / pack_num;
           const int kBlockK_params    = kBlockN / group_size;
           const int kHeadDim_pack     = kHeadDim / pack_num;
           const int kHeadDim_v_params = kHeadDim / group_size;
           const int tile_paramsk_g    = kBlockN / 32 * (kBlockN / group_size);
           const int num_params        = (num_bits == 4 ? 128 : 256) / group_size;
           // 可行性判据：全部必须为真
           bool ok = kBlockN % group_size == 0 && kHeadDim % group_size == 0
                   && residual_bs % group_size == 0 && group_size % pack_num == 0
                   && kBlockP * pack_num == kBlockN
                   && kBlockK_params * group_size == kBlockN
                   && num_params * group_size == 256 && num_params <= 8;
           printf("g=%3d: kBlockP=%d kBlockK_params=%d kHeadDim_v_params=%d "
                  "tile_paramsk_g=%d num_params=%d -> %s\n",
                  group_size, kBlockP, kBlockK_params, kHeadDim_v_params,
                  tile_paramsk_g, num_params, ok ? "OK" : "FAIL");
       }
       return 0;
   }
   ```

2. 编译运行：`g++ -std=c++17 traits_check.cpp -o traits_check && ./traits_check`。

**需要观察的现象**：三行输出中 g=64 一行以 `-> OK` 结尾，且常量值与 4.3.2 表格一致。

**预期结果**：与手算表完全一致（g=64：kBlockP=32、kBlockK_params=4、kHeadDim_v_params=2、tile_paramsk_g=32、num_params=4）。此实践不依赖 GPU，可确定性完成。

#### 4.3.5 小练习与答案

**练习 1**：group_size=16 在 2-bit 下会挂在哪条判据上？
**答案**：`group_size | kHeadDim(128)` 通过（128/16=8），`pack_num(8) | 16` 通过，但 `num_params = 256/16 = 16 > 8`——违反 qpack 归约的 reduce_tmp 容量约束（4.4.3）；同时 `4*num_params = 64` 超出 qpack traits `smem_reduce_tmp` 的 32 行第一维。所以 g=16 不该尝试。

**练习 2**：为什么判据 4 要求 `pack_num | group_size`？
**答案**：打包粒度是「一个 uint16 装 pack_num 个整数」。若一个量化组不能装下整数个 uint16（即组边界切在某个 uint16 中间），同一 uint16 的不同槽位会属于不同组、共用不了同一个 scale/zero，解码端的反量化（u5-l3 按 pack_num 选组取参数）就会取错参数。

### 4.4 硬编码雷区与验证闭环

#### 4.4.1 概念说明

4.3 证明了「编译能过」。但**派生常量自适应 ≠ 代码处处泛化**。kernel 里还有一批与 num_params / num_bits 相关的**字面常量**，它们是为已启用的配置调校的，换一个 group_size 后是否仍然正确，源码不会替你保证——好几处甚至连作者自己都标了 `TODO: check`。本模块把这些雷区列全，并给出验证闭环。

这就是「新增配置」与「新增配置后跑通正确性」之间的距离所在。

#### 4.4.2 核心流程

验证闭环（每一环都有现成工具）：

```text
解开 dispatch + 实例化 → 重新编译（install.sh）
  → evaluation/test.py 改三个常量 → 跑 prefill + 32 轮 decode
  → 看每轮 MAE：
      ├─ 与 FP16 参考同数量级（对比 g=32/128 基线）→ 通过
      ├─ MAE 巨大 / NaN → 命中硬编码雷区，按 4.4.3 逐条排查
      └─ 输出全零 → dispatch 仍落空，检查两处是否都解开
```

#### 4.4.3 源码精读

**雷区一：`qpack_kc_vt<2>` 的局部 pack_num 是算出来的还是查表查出来的？**

- [qpack.h:219-226](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L219-L226)：4-bit 特化里 `pack_num = size<1>(src) / num_params`（L225）——**由 fragment 实际形状计算**，对任意 group_size 泛化良好。
- [qpack.h:110-117](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L110-L117)：2-bit 特化里 `pack_num = 4 / (num_params / 2)`（L116）——**硬编码的 4 与整除运算**，且 L117 还有 `size<1>(src) == 4 ? num_params/2 : num_params` 的条件折半。对 g=64（num_params=4）它给出 pack_num=2，这个值是否等于真实的 `size<1>(src)/num_params` 决定 zero/scale 的索引用对没用。源码自带的 `// TODO: check 4` 与 `// seems hard code?` 注释就是作者留下的警示。**g=64 的正确性必须靠 test.py 的 MAE 来裁决，不能靠读代码断言。**

**雷区二：qpack kernel 落盘 V 参数时的索引数学。**

- [flash_fwd_kernel.h:1479-1487](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1479-L1487)：`num_params_2 = num_bits == 2 ? num_params / 2 : num_params`（L1479）之后，gV_params 的写入索引（L1484-1485）里嵌着 `128`、`64`、`8`、`4` 等字面量与 `i/8`、`j/num_params_2` 的混合运算。这套索引是为现有配置调平的；g=64 时 num_params_2=2，落盘位置是否与解码端 `load_params` 的读取位置镜像对齐，同样只能靠 MAE 验证。
- 对照 K 参数的落盘 [flash_fwd_kernel.h:1489-1497](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1489-L1497)：k-channel 路径直接用 `num_params`（L1494-1495），没有折半——K 与 V 的雷区程度不同。

**雷区三：与 num_bits 绑定的存储特判（与 group_size 无关，顺带记录）。**

- [flash_fwd_kernel.h:1509-1515](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1509-L1515)：`if (kHeadDim == 128 && num_bits == 2)` 时 V 打包数据的寄存器→smem 拷贝只让前 64 个线程参与（2-bit 寄存器产出翻倍，u5-l3 讲过原因）。此类 num_bits 特判在 g=64 下行为不变，但提醒我们：**模板参数的空间不是连续可用的，每一档都是被单独调校过的点**。

**reduce_tmp 的容量约束（判据 5 的出处）：**

- [qpack.h:56-79](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L56-L79)：`allreduce_` 对 `i < size(dst)` 逐项写 `reduce_tmp(i, ...)`。qpack kernel 中 K 的摘要张量有 `4 * num_params` 个条目（[flash_fwd_kernel.h:1451-1452](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1451-L1452)），而 qpack traits 的专用 `smem_reduce_tmp` 是 32×32（[kernel_traits.h:534-538](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L534-L549)），故要求 `4·num_params ≤ 32` 即 `num_params ≤ 8`——g=64 时 4×4=16，安全。
- 有趣的对照：解码 kernel（residual/splitkv）里 `sReduce_tmp` 不是独立存储，而是**叠加在 `smem_acc` 上的复用视图**（[flash_fwd_kernel.h:190](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L190)、[flash_fwd_kernel.h:734](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L734)），布局仅声明 8×32（[kernel_traits.h:277-282](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L277-L282)）；残余再量化路径复用这套归约（[flash_fwd_kernel.h:401-420](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L401-L420)、[flash_fwd_kernel.h:467-475](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L467-L475)）。由于 smem_acc 本身有 16×256 个元素，越界写入实际落在死区里——但这是靠「恰好够大」成立的隐性约定，改 kBlockN 时要重新审视。

#### 4.4.4 代码实践

**实践目标**：把 `evaluation/test.py` 改造成 g=64 的验证工具，并设计判读标准。

**操作步骤**：

1. 修改 [evaluation/test.py:40-45](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L40-L45) 的配置常量（示例改动，重编译后执行）：

   ```python
   quant_mode = "k-channel"
   num_bits = 2                      # 原 4
   pack_nums = 16 / num_bits         # 自动变 8
   group_size = 64                   # 原 32 —— 本讲新增的配置
   residual_block_size = 256         # 原 128 —— 必须改为 2-bit 的块大小！
   ```

   **注意 `residual_block_size` 必须同步改为 256**：这个 Python 常量决定残余区张量的分配形状（[test.py:110-113](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L110-L113) 的 `*_new` 缓冲、[test.py:127-128](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L127-L128) 的补零对齐），而 kernel 侧期望的是编译期常量 256（kernel_traits.h:75）。忘了改它，残余路径必然形状错乱。`seqlen_k=1024` 恰被 256 整除，prefill 后残余区为空（[test.py:68-69](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L68-L69)），随后 32 轮 decode 不会触发拼回——这正好把验证聚焦在 splitkv 主路径上（想要连带验证残余再量化，可把 `seqlen_k` 改成 900 这类非整除值，参照 u2-l2 的推导）。

2. 先跑 g=32 与 g=128 两组基线（各只改 `group_size` 一行），记录 32 轮 MAE 的量级；再跑 g=64。

**需要观察的现象**：g=64 的逐轮 MAE 应落在 g=32（更细分组、更准）与 g=128（更粗分组、更差）之间；三者的量级都应远小于 1（u1-l4 给过 2-bit 误差显著大于 4-bit 的结论，属正常）。

**预期结果**：`MAE(g=32) < MAE(g=64) < MAE(g=128)`，且无 NaN、无全零输出。若 g=64 的 MAE 异常大甚至 NaN，按 4.4.3 雷区一、二的顺序排查（先看 prefill 打包是否正确——单独检查 `k_pack`/`v_params` 内容，再看 decode）。**待本地验证**：本讲义写作环境无 GPU，无法代跑。

#### 4.4.5 小练习与答案

**练习 1**：为什么 g=64 的 MAE 大概率落在 g=32 与 g=128 之间？
**答案**：由 u2-l3 的有效比特模型：每元素有效比特 = num_bits + 32/group_size。同为 2-bit 时，g=64 的 2.5 bit 介于 g=32 的 3 bit 与 g=128 的 2.25 bit 之间；量化误差上界为 scale/2（u4-l3），scale 随组内 range 缩小而缩小，组越小越准。

**练习 2**：如果 g=64 编译通过、test.py 跑完 MAE 却和 FP16 参考差了几个数量级，给出你的排查顺序。
**答案**：第一步确认输出不是全零/未初始化（全零 → dispatch 落空，回去查两处注释）；第二步单独验证 qpack：调 `kvcache_pack_int` 后在 Python 里手工反量化 `k_pack`/`k_params`（用 u4-l3 的公式 x≈q·s+z）与原 FP16 K 对比，错 → 雷区一/二（qpack.h:116-117 或 flash_fwd_kernel.h:1479-1487 的索引数学）；qpack 对而 decode 错 → 查解码端取参数路径与 4.3 表中 `kHeadDim_v_params=2` 相关的布局。

## 5. 综合实践

**任务：为 group_size=64 打通 2-bit k-channel 的完整链路，并产出一份改动与验证记录。**

前置条件：按 u1-l2 完成环境准备（CUDA ≥ 11.6、cutlass 子模块已拉取、Ampere 及以上 GPU）。

按顺序执行（共 6 步，改动共 4 处、全在两个文件里，`setup.py` 无需改动）：

| 步骤 | 文件与位置 | 操作 | 目的 |
| --- | --- | --- | --- |
| 1 | [decode_api.cpp:202-203](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L202-L203) | 解开 `run_mha_fwd` 中 k-channel g=64 那行调用 | decode 注意力 dispatch |
| 2 | [decode_api.cpp:224-225](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L224-L225) | 解开 `run_kvcache_qpack` 中 k-channel g=64 那行调用 | prefill 打包 dispatch |
| 3 | [flash_fwd_split_hdim128_fp16_sm80_2bit.cu:8](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_fwd_split_hdim128_fp16_sm80_2bit.cu#L8) | 解开 `run_mha_fwd_splitkv_dispatch<..., 1, 2, 64>` 显式实例化 | 让符号存在（防步骤 1 的链接错误） |
| 4 | [flash_qpack_hdim128_fp16_sm80_2bit.cu:11-14](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_qpack_hdim128_fp16_sm80_2bit.cu#L11-L14) | 整段解开 `run_kvcache_qpack_<..., 1, 2, 64>` 显式特化 | 让符号存在（防步骤 2 的链接错误） |
| 5 | 仓库根目录 | `bash install.sh` 重新编译安装 | 生成含新实例的 `bit_decode_cuda` |
| 6 | [evaluation/test.py:40-45](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L40-L45) | `num_bits=2, group_size=64, residual_block_size=256` 后运行 | 正确性验证 |

**记录要求**（这是本实践的交付物，比「跑通」本身更重要）：

1. 编译阶段：是否出现链接错误？错误符号是什么？（若按上表顺序做，步骤 3/4 先于 5，不应出现；故意跳过步骤 3 做一次单边实验更佳。）
2. 运行阶段：逐轮 MAE 数值曲线；与 g=32 / g=128 基线的对比表。
3. 若失败：命中 4.4.3 哪个雷区？记录定位过程（例如「qpack 输出经手工反量化即错 → 锁定 qpack.h:116」）。
4. 结论：g=64 是否达到可用精度？若可用，用 u2-l3 的有效比特公式（2 + 32/64 = 2.5 bit）评估它相比 g=32 的带宽收益（参数开销从 1 bit/元素降到 0.5 bit/元素）是否值得精度损失。

无 GPU 环境的替代交付：完成步骤 1-4 的 diff、4.3.4 的常量验证程序输出，以及一份「预期验证方案」（变量、重复次数、判读标准），并明确标注运行结论**待本地验证**。

## 6. 本讲小结

- 新增量化配置必须**三层配对**：decode_api.cpp 的两处 dispatch if 链（decode 与 qpack 各一处）、genfile 里的显式实例化/特化、setup.py 源列表（仅在新建 .cu 文件时才需要动）。
- 当前仓库 12 个 dispatch 分支只启用了 4 个（k-channel × {2,4}-bit × group_size {32,128}）；k-tensor 解码与 g=64 全部处于注释状态，dispatch 落空的表现是**静默无操作**而非报错。
- split genfile 用显式实例化（主模板在头文件有定义），qpack genfile 用显式特化（主模板只有声明）；只解开 dispatch 不解实例化 → 链接错误，只解实例化 → 死代码。
- 动手前先做**编译期可行性检查**：group_size 必须整除 kBlockN(256)、kHeadDim(128)、residual_block_size，且被 pack_num 整除、num_params ≤ 8；g=64（2-bit）全部通过。
- 派生常量自适应不等于代码泛化：`qpack_kc_vt<2>` 的硬编码 pack_num（qpack.h:116，自带 TODO）、V 参数落盘索引（flash_fwd_kernel.h:1479-1487）是为既有配置调校的，新配置的正确性必须用 test.py 的 MAE 闭环裁决。

## 7. 下一步学习建议

- **u7-l4（下一篇）**：把这些「被禁用分支与硬编码」放到架构评审的框架下——为什么 k-tensor 解码被整体注释？block_n=256 写死带来什么代价？本讲的 g=64 实验正好是评审报告的第一手素材。
- **进阶阅读**：对照 [flash.h:203-205](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L203-L205) 的三个声明与上游 FlashAttention 的 `FP16_SWITCH/HEADDIM_SWITCH` 宏式 dispatch（decode_api.cpp:185-195 保留着被注释的原版），思考「宏展开生成 if 链」与「手写 if 链」在可维护性上的取舍——若要支持几十种配置组合，你会怎么重写 dispatch？
- **动手方向**：若 g=64 顺利通过验证，可尝试更有野心的扩展——为 4-bit 启用 g=64（对照 4.2.3 的 4-bit 文件结构），或评估把 `run_kvcache_qpack_hdim128` 中 `kBlockN` 的 num_bits 三元表达式（flash_fwd_launch_template.h:203）参数化后对实例化矩阵的影响。

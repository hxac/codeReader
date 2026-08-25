# 仓库结构与代码地图：Python 包、CUDA 扩展与评测目录

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 BitDecoding 仓库三个主要目录（`bit_decode/`、`csrc/bit_decode/`、`evaluation/`）各自的角色与语言层级（Python / C++ / CUDA C++）。
2. 指出 Python 侧仅有的两个核心 API：`kvcache_pack_int`（打包）与 `fwd_kvcache_int`（解码），并说出它们内部如何按 `num_bits` 分流到 `int2`/`int4` 绑定函数。
3. 画出从 Python 接口 → pybind11 绑定 → 参数结构体 → kernel 启动模板 → GPU kernel 的分层依赖图，并能区分 prefill（量化打包）与 decode（低比特注意力）两条调用链。

本讲是「地图课」：不深入任何一个 kernel 的数学细节，而是把整个仓库的骨架搭起来，后续单元会逐层下潜。

## 2. 前置知识

### 2.1 Python 扩展模块与 pybind11

科学计算项目常见的一种分层结构是：

- **算法核心**用 C++/CUDA 写（性能）；
- **用户接口**用 Python 写（易用）；
- 两者之间靠**绑定层**（binding）连接。本项目使用的绑定工具是 pybind11，它用 `PYBIND11_MODULE` 宏把 C++ 函数注册成 Python 模块里的函数。

在第 1 讲我们已知：本项目的 CUDA 扩展编译后叫 `bit_decode_cuda`，Python 里 `import bit_decode_cuda` 即可调用其中的 C++ 函数。

### 2.2 prefill 与 decode 两个阶段

自回归 LLM 推理分两个阶段：

- **prefill（预填充）**：一次性处理整段提示词，`q_len > 1`。此时 KV cache 还不存在，本阶段要做的事情之一是「把算出来的 FP16 K/V 量化打包进缓存」。
- **decode（解码）**：每步只处理 1 个新 token，`q_len == 1`。此阶段用低比特 KV cache 直接做注意力计算，是 BitDecoding 加速的主战场。

### 2.3 KV cache 与低比特打包（回顾）

第 1 讂已建立的概念，本讲直接使用：

- KV cache 是 decoding 阶段反复读取的大张量，读取带宽是瓶颈（算术强度约 \( \text{AI} \approx 1\ \text{FLOP/Byte} \)，远低于 GPU 的浮点吞吐与带宽之比，属于 memory-bound）。
- BitDecoding 把 K/V 量化成 2/4-bit 并按 `pack_nums = 16 / num_bits` 个元素压进一个 `uint16` 容器，读取字节降为原来的 1/4 或 1/8。
- 最新 token 保存在 FP16 **残余（residual）区**，攒满一个 `residual_block_size` 再量化拼回主缓存。

### 2.4 FlashAttention 与 CUTLASS/CuTe 一句话版

- **FlashAttention**：用分块（tiling）+ 在线 softmax 避免实例化完整注意力矩阵的 GPU kernel 家族，本项目源码由其 SM80 版本改造而来（文件头部的 Tri Dao 版权注释保留了这一渊源）。
- **CUTLASS/CuTe**：NVIDIA 的 C++ 模板库，提供布局（Layout）、张量（Tensor）、MMA（Tensor Core 矩阵乘）抽象，位于 `libs/cutlass` 子模块（第 2 讲已介绍其获取方式）。

## 3. 本讲源码地图

先给出整体目录视图（仅列本讲涉及的文件，`libs/` 子模块与构建产物省略）：

```text
OpenBitSys-BitDecoding/
├── setup.py / install.sh / requirements.txt / .gitmodules   # 构建（第 2 讲已讲）
├── bit_decode/                    # 【Python 包】用户接口层
│   ├── __init__.py                #   门面：导出 2 个函数 + 3 个缓存类
│   ├── bit_decode_interface.py    #   两个核心 API 的薄封装（本讲 4.1）
│   └── models/
│       └── cache_utils.py         #   改造版 HF DynamicCache（本讲只看角色）
├── csrc/bit_decode/               # 【CUDA 扩展】C++/CUDA 层
│   ├── decode_api.cpp             #   pybind11 绑定 + 运行时 dispatch（本讲 4.2）
│   └── src/
│       ├── flash_fwd_launch_template.h  # kernel 定义与启动模板（本讲 4.2）
│       ├── flash_fwd_kernel.h     #   kernel 设备端实现（后续单元精读）
│       ├── flash_api.h            #   改造前 FA2 风格的宿主 API 头（保留文件）
│       ├── include/flash.h        #   Flash_fwd_params 参数结构体
│       ├── include/kernel_traits.h / qpack.h / dequantize.h / ...  # kernel 骨架与原语
│       ├── genfile/*.cu           #   5 个模板显式实例化编译单元
│       └── test_*.cu / bench_*.cu #   独立 CUDA 测试与微基准（第 7 单元）
└── evaluation/                    # 【评测目录】模型接入与实验
    ├── llama.py / qwen3.py        #   改造版 HF 模型文件（本讲 4.3）
    ├── example.py                 #   GSM8K 长上下文生成入口（含猴子补丁）
    ├── test.py                    #   kernel 级正确性冒烟测试
    ├── bench_throughput.py        #   吞吐基准
    ├── scripts/                   #   shell 启动脚本
    └── ablation/                  #   对比其他低比特方案的基线
```

本讲精读的 4 个关键文件：

| 文件 | 目录 | 语言 | 作用 |
| --- | --- | --- | --- |
| [bit_decode/bit_decode_interface.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py) | `bit_decode/` | Python | 两个核心 API 的薄封装，按 `num_bits` 分流 |
| [csrc/bit_decode/decode_api.cpp](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp) | `csrc/bit_decode/` | C++ | pybind11 导出 4 个函数；形状校验、参数结构体填充、模板 dispatch |
| [csrc/bit_decode/src/flash_fwd_launch_template.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h) | `csrc/bit_decode/src/` | C++/CUDA | 定义 4 个 `__global__` kernel 与启动函数（grid 计算、共享内存设置） |
| [evaluation/llama.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py) | `evaluation/` | Python | 改造版 Llama：`LlamaBitDecoding` 注意力类，prefill/decode 双路径 |

## 4. 核心概念与源码讲解

### 4.1 模块一：`bit_decode/` Python 包 —— 两层即可触达 CUDA 的门面

#### 4.1.1 概念说明

`bit_decode/` 是安装后用户直接 `import` 的 Python 包。它的设计哲学是「极薄门面」：

- 整个包只导出**两个 kernel 入口函数**（`kvcache_pack_int`、`fwd_kvcache_int`）和**三个缓存类**（`Cache`、`DynamicCache`、`StaticCache`）。
- 函数本体不做任何计算，只做两件事：(1) 把四维张量 reshape 成绑定层期望的形状；(2) 按 `num_bits` 是 4 还是 2，选择调用 `bit_decode_cuda` 里的 `*_int4` 或 `*_int2` 函数。

为什么需要这层薄封装而不是让用户直接调 `bit_decode_cuda`？因为 C++ 模板函数 `mha_fwd_kvcache<num_bits>` 在编译期固定 `num_bits`，必须实例化成两个独立函数导出；而 Python 侧希望用同一个函数名 + 一个 `num_bits` 参数。这层封装就是「编译期模板 → 运行期参数」的翻译层。

#### 4.1.2 核心流程

`import bit_decode` 时发生的事：

```text
import bit_decode
  └─ 执行 bit_decode/__init__.py
       ├─ from bit_decode.bit_decode_interface import kvcache_pack_int, fwd_kvcache_int
       │    └─ 执行 bit_decode/bit_decode_interface.py
       │         └─ import bit_decode_cuda   ← 级联加载编译好的 CUDA 扩展（.so）
       └─ from bit_decode.models.cache_utils import Cache, DynamicCache, StaticCache
```

两个 API 的内部分流逻辑（伪代码）：

```text
kvcache_pack_int(k_cache, k_pack, k_params, v_cache, v_pack, v_params, ...,
                 quant_mode, group_size, num_bits):
    K_unpad = k_cache.reshape(b * seqlen_k, nheads_k, d)      # 摊平 batch 维
    V_unpad = v_cache.reshape(b * seqlen_k, nheads_k, d)
    if num_bits == 4: bit_decode_cuda.kvcache_pack_int4(K_unpad, ..., group_size)
    elif num_bits == 2: bit_decode_cuda.kvcache_pack_int2(K_unpad, ..., group_size)
    else: raise ValueError

fwd_kvcache_int(q, k_pack, k_params, v_pack, v_params, ..., num_bits):
    if num_bits == 4: 调 bit_decode_cuda.fwd_kvcache_int4(...，固定尾参 7 个)
    elif num_bits == 2: 调 bit_decode_cuda.fwd_kvcache_int2(...，固定尾参 7 个)
    return out_bit, k_pack_new, k_params_new, v_pack_new, v_params_new
```

#### 4.1.3 源码精读

**入口导出。** 包的门面只有 8 行，导出的就是前文所说的「2 函数 + 3 类」：

[bit_decode/__init__.py:1-8](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/__init__.py#L1-L8) —— 定义版本号 `1.0.0.post1`，并从子模块导入两个函数与三个缓存类。这 5 个名字就是 Python 侧全部的公开 API。

**CUDA 扩展的加载点。** 注意源码注释「We need to import the CUDA kernels after importing torch」——先 `import torch` 再 `import bit_decode_cuda`，否则扩展加载时找不到 torch 的符号：

[bit_decode/bit_decode_interface.py:8-10](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L8-L10) —— 第 10 行 `import bit_decode_cuda as bit_decode_cuda` 是整个 Python 世界与 CUDA 扩展的唯一接触点。

**打包函数的 reshape 与分流。** `kvcache_pack_int` 把 `(b, seqlen_k, h, d)` 摊平成 `(b*seqlen_k, h, d)`（`cu_seqlens_k` 前缀和数组此时承担 batch 边界信息），然后按 `num_bits` 分流：

[bit_decode/bit_decode_interface.py:21-45](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L21-L45) —— 第 23-24 行做 reshape；第 26-43 行分别是 `int4` 与 `int2` 分支；第 44-45 行对其他 `num_bits` 抛出 `ValueError`（所以不支持 3-bit、8-bit 等其他位宽）。

**解码函数的固定尾参。** `fwd_kvcache_int` 的 C++ 侧签名比 Python 侧长出 7 个参数，Python 封装把它们写死：

[bit_decode/bit_decode_interface.py:63-82](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L63-L82) —— 传给 `fwd_kvcache_int4` 的尾参依次是 `False, -1, -1, 0.0, True, 0`，源码里用 `# Added` 注释标出。对照 4.2 节的 C++ 签名可知它们对应 `is_causal=False`、`window_size_left=-1`、`window_size_right=-1`、`softcap=0.0`、`is_rotary_interleaved=True`、`num_splits=0`（0 表示交给启发式自动决定切分数）。解码时 `seqlen_q==1` 天然无因果掩码问题，`is_causal=False` 是合理的。

**返回值。** 函数返回 5 个张量：

[bit_decode/bit_decode_interface.py:107](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L107) —— `out_bit` 是注意力输出；后 4 个 `*_new` 张量是「残余区攒满一块时，kernel 顺手产出的新量化块」，由调用方写回主缓存（见 4.3 节）。

#### 4.1.4 代码实践：用内省确认「只有两个入口」

1. **实践目标**：验证 `bit_decode` 包与 `bit_decode_cuda` 扩展的公开成员，确认 Python 侧入口确实只有两个函数。
2. **操作步骤**（需要已完成第 2 讲的安装；无 GPU 环境则跳到步骤 3）：

   ```bash
   python -c "
   import bit_decode
   print(bit_decode.__version__)
   print([n for n in dir(bit_decode) if not n.startswith('_')])
   import bit_decode_cuda
   print([n for n in dir(bit_decode_cuda) if not n.startswith('_')])
   "
   ```

3. **需要观察的现象**：第一个列表应包含 `kvcache_pack_int`、`fwd_kvcache_int`、`Cache`、`DynamicCache`、`StaticCache`；第二个列表应包含 `kvcache_pack_int2`、`kvcache_pack_int4`、`fwd_kvcache_int2`、`fwd_kvcache_int4`——正好与 4.2 节 `PYBIND11_MODULE` 注册的 4 个名字一一对应。
4. **预期结果**：两个列表的函数名合并去重后只有 pack/fwd 两类，证明「Python 侧仅有的两个核心 API」。
5. 无 GPU / 未安装时，可改用纯源码方式验证：`grep -n "def " bit_decode/bit_decode_interface.py` 只会列出上述两个函数定义（待本地验证运行输出）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fwd_kvcache_int` 要返回 4 个 `*_new` 张量，而不是由 kernel 直接追加进主缓存？

**答案**：CUDA kernel 不方便做动态内存分配/张量拼接；主缓存 `k_pack` 等由 Python 侧持有并管理形状。kernel 把「攒满一个 residual 块后新量化的结果」写到预先分配好的 `*_new` 缓冲里返回，由 Python 侧的 `DynamicCache.update_pack` 负责 concat 拼接（见 4.3 节 llama.py 第 681-683 行），职责分离且缓冲可跨步复用。

**练习 2**：如果调用 `kvcache_pack_int(..., num_bits=3)` 会发生什么？在哪一行触发？

**答案**：抛出 `ValueError: Unsupported num_bits=3; expected 2 or 4`，触发点在 [bit_decode/bit_decode_interface.py:44-45](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L44-L45)。根因是 C++ 侧只实例化了 `<2>` 和 `<4>` 两个模板版本（见 4.2 节与 `genfile/`）。

**练习 3**：`import bit_decode` 失败报 `ModuleNotFoundError: No module named 'bit_decode_cuda'`，最可能的原因是什么？

**答案**：CUDA 扩展没有编译或不在 Python 路径上。回顾第 2 讲：本项目没有预编译 wheel，`bit_decode_cuda` 由 `setup.py` 的 `CUDAExtension` 现场编译，需要先成功执行 `bash install.sh`（并确认 `libs/cutlass` 子模块已拉取）。

### 4.2 模块二：`csrc/bit_decode/` CUDA 扩展 —— 绑定、参数结构体与 kernel 启动

#### 4.2.1 概念说明

这个目录是项目的算法核心，从上到下分四层：

| 层 | 文件 | 语言 | 职责 |
| --- | --- | --- | --- |
| 绑定层 | `decode_api.cpp` | C++（宿主端） | pybind11 导出、输入校验、填充参数结构体、选择模板实例 |
| 参数层 | `src/include/flash.h` | C++ | `Flash_fwd_params`：一个同时被 CPU 和 GPU 代码读取的 POD 结构体 |
| 启动层 | `src/flash_fwd_launch_template.h` | C++/CUDA | 计算 grid、设置共享内存、发起 `<<<>>>` kernel 启动 |
| 设备层 | `src/flash_fwd_kernel.h` + `src/include/*` | CUDA C++（设备端） | kernel 本体与量化/反量化/归约原语 |

**关键设计：`num_bits`、`quant_mode`、`group_size` 是编译期模板参数。** 同一套 kernel 源码按 `<量化模式, 位宽, 分组大小>` 的组合实例化出多个二进制版本，运行时由 `decode_api.cpp` 里的 `if` 链选择。这就是为什么仓库里存在 `src/genfile/` 目录——第 2 讲讲过的 5 个 `.cu` 编译单元负责「显式实例化」，让链接器找得到对应符号。

另一个值得注意的工程事实：**并非所有组合都被启用**。`run_mha_fwd` 中 k-tensor 模式的 splitkv 分支、`group_size=64` 分支目前被注释掉，只有 k-channel + group_size 128/32 的组合在运行（这是第 7 单元「新增配置」实践的伏笔）。

#### 4.2.2 核心流程

以 decode 阶段一次 `fwd_kvcache_int(q, ..., num_bits=4)` 调用为例，C++ 侧的完整路径：

```text
bit_decode_cuda.fwd_kvcache_int4(...)                     # pybind 导出名
  └─ mha_fwd_kvcache<4>(q, k_pack, k_params, v_pack, v_params,
                         k_new, v_new, seqlens_k,
                         k_pack_new, ..., v_params_new, ...)
       ├─ 1. GPU 架构检查（仅 sm80+）
       ├─ 2. 从张量尺寸推导 batch_size / seqlen_k / num_heads_k
       ├─ 3. GQA 优化：seqlenq_ngroups_swapped 重排 q
       ├─ 4. set_params_fprop(...)          # 填 Flash_fwd_params（指针+stride+尺寸）
       ├─ 5. 若传入 k_new/v_new：填残余/新增 KV 的指针与 *_new 输出 stride
       ├─ 6. set_params_splitkv(...)         # num_splits_heuristic 选切分数，+1 给残余 kernel；
       │                                       分配 softmax_lse_accum / out_accum 缓冲
       └─ 7. run_mha_fwd<4>(params, stream, force_split_kernel=true)
            └─ 按 quant_mode/group_size 选模板实例（当前启用 k-channel 128/32）
                 └─ run_mha_fwd_splitkv_dispatch<half_t, 128, false, 1, 4, 128>
                      └─ run_flash_splitkv_fwd<Kernel_traits>
                           ├─ 启动 flash_fwd_residual_kernel   # 处理 FP16 残余区+新 token
                           ├─ 启动 flash_fwd_splitkv_kernel    # 逐块处理打包 KV
                           └─ 启动 flash_fwd_splitkv_combine_kernel  # LSE 合并
```

prefill 阶段的打包链则短得多：

```text
bit_decode_cuda.kvcache_pack_int4(...)
  └─ kvcache_qpack<4>(k, k_pack, k_params, v, v_pack, v_params, ...)
       ├─ 校验 dtype/device/stride
       ├─ set_params_fprop_qpack(...)       # FP16 输入 + 打包输出的指针与 stride
       └─ run_kvcache_qpack<4>(params, stream)
            └─ run_kvcache_qpack_<half_t, 128, quant_mode, 4, group_size>
                 └─ run_flash_qpack<Flash_qpack_traits>   # grid=(num_n_block, b, h)
                      └─ flash_qpack_kernel → flash::compute_qpack<...>
```

#### 4.2.3 源码精读

**pybind11 注册表——CUDA 扩展的「目录页」。** 整个扩展只导出 4 个函数，全部是模板实例化后的包装：

[csrc/bit_decode/decode_api.cpp:688-694](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L688-L694) —— `PYBIND11_MODULE(TORCH_EXTENSION_NAME, m)` 把 `kvcache_qpack<2>`/`<4>` 注册为 `kvcache_pack_int2`/`int4`，把 `mha_fwd_kvcache<2>`/`<4>` 注册为 `fwd_kvcache_int2`/`int4`。`m.doc() = "BitDecoding"` 说明模块文档名。这张 4 行的表就是 Python 与 C++ 世界的全部接口契约。

**解码入口的签名与防御性校验。** `mha_fwd_kvcache` 是模板函数，参数极多（q、4 个打包缓存张量、可选的 k_/v_/seqlens_k_、4 个 `*_new` 输出、block_table，以及 softmax_scale/quant_mode/group_size/residual_block_size/new_lens 等）：

[csrc/bit_decode/decode_api.cpp:315-341](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L315-L341) —— 函数签名。注意第 331-340 行的默认值 `quant_mode="k-tensor"`、`group_size=128`、`num_splits=0`，与 4.1 节 Python 封装传的固定尾参一一对应。

[csrc/bit_decode/decode_api.cpp:343-347](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L343-L347) —— 第一步就检查 GPU 架构：`TORCH_CHECK(is_sm90 || is_sm8x, ...)`，非 Ampere 及以上直接报错。文件开头的三个宏 `CHECK_DEVICE`/`CHECK_SHAPE`/`CHECK_CONTIGUOUS`（[decode_api.cpp:19-21](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L19-L21)）是所有输入张量的防御性校验工具。

**GQA 重排优化。** 解码时 `seqlen_q==1` 且查询头数多于 KV 头数（grouped-query attention），把 heads 维与 seqlen 维交换能让一个 block 同时算多个查询头：

[csrc/bit_decode/decode_api.cpp:385-391](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L385-L391) —— 满足条件时把 q 从 `(b, 1, nheads, d)` 重排为 `(b, ngroups, nheads_k, d)`，`seqlen_q` 变为 `ngroups`。

**参数结构体填充。** CPU 与 GPU 之间不传几十个散装参数，而是填一个结构体一次性传给 kernel：

[csrc/bit_decode/decode_api.cpp:415-438](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L415-L438) —— 调用 `set_params_fprop`，把尺寸（b/h/h_k/seqlen 等）、所有张量的 `data_ptr()`、softmax scale、量化配置写进 `Flash_fwd_params params`。

[csrc/bit_decode/src/include/flash.h:102](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L102) —— `struct Flash_fwd_params : public Qkv_params` 的定义处（`Qkv_params` 基类在 [flash.h:24](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L24)）。相比原版 FlashAttention，它增加了 `K_pack_ptr`/`k_params_ptr` 等量化缓存指针与 stride 字段（第 3 单元第 2 讲会逐字段精读）。

**残余/新增 KV 的处理。** 传入 `k_`/`v_`（FP16 的新 token 与残余缓存）时，填入 `knew_ptr`、`*_new` 输出指针及其四级 stride：

[csrc/bit_decode/decode_api.cpp:441-500](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L441-L500) —— 第 475 行 `const int pack_nums = 16 / num_bits;` 是位宽换算的核心常量；第 477-498 行把 `k_pack_new` 等 4 个输出缓冲的指针与 stride 写入 params，kernel 将在残余攒满时把新量化块写到这些地址。

**split 数选择与强制 split。** 解码时 batch×heads 很小而 KV 很长，直接启动会导致 GPU 大部分 SM 闲置，所以把 KV 维切开：

[csrc/bit_decode/decode_api.cpp:282-313](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L282-L313) —— `set_params_splitkv` 中，`num_splits < 1` 时调用 `num_splits_heuristic`（函数体在 [decode_api.cpp:246-280](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L246-L280)，按占用率波形效率选切分数）；第 303 行 `params.num_splits += 1;` 注释写明「We need to add 1 for residual kernel」——额外的第 0 号 split 留给 FP16 残余 kernel；第 306-307 行分配 LSE 与输出的 float 累积缓冲。

[csrc/bit_decode/decode_api.cpp:512-519](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L512-L519) —— 最后以 `force_split_kernel=true` 调用 `run_mha_fwd`（注释说明：只有 split kernel 支持追加 KV cache 与 paged KV）。

**运行时 dispatch——模板参数的「选择器」。** `run_mha_fwd<num_bits>` 用 `if` 链把运行期字符串/整数映射到编译期模板实例：

[csrc/bit_decode/decode_api.cpp:196-216](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L196-L216) —— 第 199 行起：`quant_mode == "k-channel"` 时启用 `group_size` 128（第 201 行）与 32（第 205 行）两个分支；`group_size == 64`（第 203 行）与整个 k-tensor 分支（第 207-214 行）都被注释。模板实参顺序为 `<半精度类型, headdim=128, Is_causal=false, quant_mode(1=k-channel), num_bits, group_size>`。

[csrc/bit_decode/decode_api.cpp:219-238](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L219-L238) —— 打包侧的 `run_kvcache_qpack<num_bits>` 同构：只有 k-channel + 32/128 被启用。

**显式实例化——让选择器选得到符号。** dispatch 里引用的模板函数必须有实体，`genfile/` 负责：

[csrc/bit_decode/src/genfile/flash_fwd_split_hdim128_fp16_sm80_4bit.cu:12-14](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_fwd_split_hdim128_fp16_sm80_4bit.cu#L12-L14) —— 显式实例化 `run_mha_fwd_splitkv_dispatch<half_t, 128, false, 1, 4, 128>` 与 `<..., 1, 4, 32>`，恰好对应 dispatch 启用的两个分支；被注释的第 7-13 行则是未启用的组合。文件头注释说明拆分多文件是为了加速编译。

**kernel 定义与三连启动。** 启动模板文件定义了 4 个 `__global__` kernel 并提供启动函数：

[csrc/bit_decode/src/flash_fwd_launch_template.h:33-39](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L33-L39) —— `flash_qpack_kernel`，prefill 量化打包 kernel，设备端入口是 `flash::compute_qpack`。

[csrc/bit_decode/src/flash_fwd_launch_template.h:50-64](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L50-L64) —— `flash_fwd_residual_kernel`（FP16 残余区 + 新 token + 原位再量化）与 `flash_fwd_splitkv_kernel`（逐块处理打包 KV），二者是 decode 阶段的主力。

[csrc/bit_decode/src/flash_fwd_launch_template.h:66-69](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L66-L69) —— `flash_fwd_splitkv_combine_kernel`，把多个 split 的部分结果按 LSE 权重合并。

[csrc/bit_decode/src/flash_fwd_launch_template.h:76-105](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L76-L105) —— `run_flash_splitkv_fwd` 是三连启动的编排者：第 85-96 行以 `grid_res=(num_m_block, b, h)` 启动残余 kernel；第 98-104 行以 `grid=(num_m_block, num_splits-1, b*h)` 启动 splitkv kernel（注意 `num_splits_ = params.num_splits - 1`，减掉的正是残余 kernel）；两处都检查共享内存超过 48KB 时调用 `cudaFuncSetAttribute` 放宽上限。

[csrc/bit_decode/src/flash_fwd_launch_template.h:106-127](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L106-L127) —— 当 `num_splits > 1` 时按切分数量选择 `Log_max_splits` 模板参数（1~7 对应 2~128 个 split），启动 combine kernel。

[csrc/bit_decode/src/flash_fwd_launch_template.h:130-137](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L130-L137) —— `run_mha_fwd_splitkv_dispatch` 把 dispatch 传来的模板参数组装成 `Flash_fwd_kernel_traits<Headdim, kBlockM=16, kBlockN=256, ...>` 并调用启动编排函数。

[csrc/bit_decode/src/flash_fwd_launch_template.h:180-206](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L180-L206) —— 打包侧：`run_flash_qpack` 以 `grid=(num_n_block, b, h)` 启动 qpack kernel；`run_kvcache_qpack_hdim128` 按 `num_bits` 选 `kBlockN`（4-bit 为 128、2-bit 为 256）并组装 `Flash_qpack_traits`。

#### 4.2.4 代码实践：手工对齐 Python 与 C++ 的参数表

1. **实践目标**：不看运行结果，纯靠源码把 `fwd_kvcache_int4` 的 Python 实参逐一对应到 `mha_fwd_kvcache<4>` 的 C++ 形参，验证「薄封装只做翻译」这一论断。此实践无需 GPU。
2. **操作步骤**：
   - 打开 [bit_decode/bit_decode_interface.py:63-82](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L63-L82)，从 `q` 开始按顺序列出全部 26 个实参。
   - 打开 [csrc/bit_decode/decode_api.cpp:317-341](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L317-L341)，按位置对齐形参，做一张两列对照表。
   - 特别标注 7 个 `# Added` 尾参（`False, -1, -1, 0.0, True, 0`）落在哪些形参上。
3. **需要观察的现象**：对照表中不应出现任何「对不上」的空位；`new_lens` 之后的所有实参都是 Python 侧写死的。
4. **预期结果**（参考答案）：`False→is_causal`、`-1→window_size_left`、`-1→window_size_right`、`0.0→softcap`、`True→is_rotary_interleaved`、`0→num_splits`。其中 `num_splits=0` 会在 [decode_api.cpp:297-302](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L297-L302) 触发启发式自动选择切分数。
5. 此结论完全来自静态源码，可直接核对；若想运行验证（如在绑定层加打印），需本地编译环境（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`params.num_splits` 为什么要 `+= 1`？启动 splitkv kernel 时又为什么用 `num_splits - 1`？

**答案**：切分启发式只规划「打包 KV 区」的切分数；实际还需要一个专门的 kernel 处理 FP16 残余区与新 token，所以总数加 1（[decode_api.cpp:303](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L303)）。启动时 `num_splits_ = params.num_splits - 1`（[flash_fwd_launch_template.h:83](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L83)）就是减去残余 kernel 占用的那一份，残余 kernel 用独立的 `grid_res` 启动。

**练习 2**：dispatch 里 `run_mha_fwd_splitkv_dispatch<cutlass::half_t, 128, false, 1, num_bits, 128>` 的 6 个模板实参各是什么含义？

**答案**：依次为：元素类型 `cutlass::half_t`（FP16）、head_dim=128、`Is_causal=false`（解码单查询无需因果掩码）、`quant_mode=1`（整数 1 编码 k-channel 模式）、`num_bits`（2 或 4）、`group_size=128`。对照 [flash.h:204](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L204) 的模板声明可确认参数顺序。

**练习 3**：如果把 `quant_mode="k-tensor"`、`group_size=128` 传给 `fwd_kvcache_int`，会发生什么？

**答案**：不会报编译错误，但 dispatch 进入 `else` 分支后所有子分支都被注释（[decode_api.cpp:207-215](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L207-L215)），`run_mha_fwd` 静默不启动任何 split kernel，输出未被写入——这正是一个「分支未启用却无报错」的陷阱，也是第 7 单元要动手补齐的路径。

### 4.3 模块三：`evaluation/` —— 把 kernel 接进真实模型

#### 4.3.1 概念说明

前两个模块构成一个「低比特注意力算子库」。`evaluation/` 回答的问题是：**怎么让一个真实的 HuggingFace LLM 用上这个算子？**

作者的策略不是侵入式修改 transformers 库，而是：

1. **复制模型文件**：把 `transformers` 的 `llama.py`/`qwen3.py` modeling 文件复制到本目录，在原有注意力类（`LlamaAttention`、`LlamaFlashAttention2`、`LlamaFlashDecodingAttention`）之外新增 `LlamaBitDecoding` 类，并注册进 `LLAMA_ATTENTION_CLASSES` 字典；
2. **替换缓存类**：新增一个扩展版 `DynamicCache`（在 `bit_decode/models/cache_utils.py`），能同时管理「打包低比特主缓存」和「FP16 残余缓存」；
3. **猴子补丁**：入口脚本在 import 模型文件前，把 `transformers.cache_utils` 里的三个缓存类替换为自己的版本，使官方 `generate()` 流程无感切换。

目录里还包含正确性测试（`test.py`，不加载大模型）、端到端生成示例（`example.py` + `scripts/example.sh`）、吞吐基准（`bench_throughput.py`）和消融基线（`ablation/`）。

#### 4.3.2 核心流程

`LlamaBitDecoding.forward` 按 `q_len` 走两条路径（这正是本讲开头说的两条调用链在模型层的汇合点）：

```text
输入 hidden_states (b, q_len, hidden_size)
  ├─ q/k/v 投影 + RoPE + transpose 到 (b, seq, heads, dim)
  │
  ├─ q_len == 1 ：decode 路径
  │    ├─ update_pack(None,...)         # 只读取打包主缓存 k_pack/k_params/v_pack/v_params
  │    ├─ update_residual(k_new, v_new) # FP16 残余缓存追加本步新 token，返回当前残余长度
  │    ├─ 残余缓存拷入补零的固定大小缓冲 (b, residual_block_size, h, d)
  │    ├─ fwd_kvcache_int(...)          # → 4.1 → 4.2 的解码 kernel 链
  │    └─ 若 cur_residual_len == residual_block_size:
  │         update_pack(*_new) + clear_residual()   # 新量化块拼回主缓存，清空残余区
  │
  └─ q_len > 1 ：prefill 路径
       ├─ _flash_attention_forward(...)             # 标准 FP16 flash-attn 算注意力
       ├─ 按 quant_mode 分配 k_pack/k_params/v_pack/v_params
       ├─ 尾部 residual_len 个 token 切出存 update_residual
       ├─ kvcache_pack_int(...)                     # → 4.1 → 4.2 的打包 kernel 链
       ├─ update_pack(...)                          # 打包结果写入主缓存
       └─ 预分配 self.*_new 四个输出缓冲（供 decode 阶段复用）
```

#### 4.3.3 源码精读

**模型文件的导入——两条调用链的起点。** 改造版 llama.py 顶部同时导入官方 flash-attn、bit_decode 接口与改造版缓存：

[evaluation/llama.py:54-57](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L54-L57) —— 第 55 行 `from bit_decode import kvcache_pack_int, fwd_kvcache_int` 是 decode/prefill 两条链的共同入口；第 56 行导入改造版缓存类，第 57 行被注释掉的官方导入直观展示了「替换」这一动作。

**量化配置如何进入注意力层。** 配置字段在所有注意力类的基类构造函数中读取：

[evaluation/llama.py:286-290](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L286-L290) —— 从 `config` 读取 `num_bits`、`quant_mode`、`group_size`、`residual_block_size`，并计算 `pack_nums = 16 / num_bits`。这些字段由 `example.py` 等入口在加载模型前注入 config（第 6 单元详讲）。

**decode 分支（q_len == 1）。** 这是「每生成一个 token 都要走一遍」的热路径：

[evaluation/llama.py:648-663](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L648-L663) —— 第 649 行从缓存读取打包张量；第 656-657 行分配固定大小 `residual_block_size` 的补零缓冲；第 658 行 `update_residual` 把新 token 追加进残余缓存并取回当前长度；第 662-663 行把残余缓存拷入缓冲前部。

[evaluation/llama.py:666-679](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L666-L679) —— 调用 `fwd_kvcache_int`：传入 Q、4 个打包张量、补零后的残余缓冲、`self.k_pack_new` 等复用缓冲、`1/sqrt(d)` 的 softmax scale，以及量化配置；`new_lens` 传 `cur_residual_len` 告诉 kernel 残余区实际有效长度。

[evaluation/llama.py:681-683](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L681-L683) —— 残余攒满一块时：`update_pack(self.k_pack_new, ...)` 把 kernel 产出的新量化块拼接进主缓存，`clear_residual` 清空残余区，下一个 token 从 0 重新累积。第 685-687 行留着被注释的调试打印（本讲综合实践会启用类似打印）。

**prefill 分支（q_len > 1）。**

[evaluation/llama.py:689-703](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L689-L703) —— 先用标准 FP16 flash-attn 算注意力（这一步输出不受量化影响，保证提示词阶段的注意力精度）。

[evaluation/llama.py:705-732](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L705-L732) —— 计算尾部不足一块的 `residual_len`，按 `quant_mode` 分配两种布局的打包张量（k-channel 与 k-tensor 的形状差异是第 2 单元第 1 讲的主题），尾部 token 切出存入残余缓存。

[evaluation/llama.py:734-745](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L734-L745) —— 调用 `kvcache_pack_int` 完成量化打包，随后 `update_pack` 写入主缓存。

[evaluation/llama.py:747-750](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L747-L750) —— 预分配 `self.k_pack_new` 等 4 个缓冲，供后续每步 decode 的 kernel 输出复用（避免反复分配显存）。

**注意力后端注册表。** 配置里的 `attn_backend` 字符串如何变成具体的类？

[evaluation/llama.py:761-766](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L761-L766) —— `LLAMA_ATTENTION_CLASSES` 字典注册了 4 个后端：`eager`、`flash_attention_2`、`flash_decoding`（基线）与 `bit_decoding`（本项目）。

[evaluation/llama.py:769-774](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L769-L774) —— `LlamaDecoderLayer.__init__` 用 `LLAMA_ATTENTION_CLASSES[config.attn_backend](...)` 实例化注意力模块——这就是后端切换机制的实现，`qwen3.py` 中有同构的注册表（第 6 单元对照阅读）。

**入口脚本的猴子补丁。**

[evaluation/example.py:8-16](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L8-L16) —— 第 9-11 行把 `transformers.cache_utils` 的 `DynamicCache`/`StaticCache`/`Cache` 替换为 bit_decode 版本；第 14-15 行从**本目录**（而非 transformers 库）导入改造版 `LlamaForCausalLM`/`Qwen3ForCausalLM`。替换必须发生在 import 模型文件之前，顺序不能颠倒。

**正确性冒烟测试。** `test.py` 不加载任何大模型，直接验证 kernel 链路的数值正确性：

[evaluation/test.py:13-37](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L13-L37) —— `attention_ref` 用 einsum + softmax 实现 FP16 参考注意力，是与 kernel 输出对比的「标准答案」。

[evaluation/test.py:97-107](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L97-L107) —— Round 1（prefill）：切分残余、调用 `kvcache_pack_int`、`update_pack` 入缓存——与 llama.py 的 prefill 分支逐行同构，是理解模型代码的最小复刻。

[evaluation/test.py:116-160](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L116-L160) —— Round 2-33（decode 循环）：每轮追加 1 个新 kv 进残余区，调用 `fwd_kvcache_int`，攒满时回写并清空残余，最后与 `attention_ref` 对比打印平均绝对误差。第 1 单元第 4 讲会实际运行它。

#### 4.3.4 代码实践：给 `LlamaBitDecoding.forward` 加两行调试打印

1. **实践目标**：亲眼确认 prefill/decode 双路径的存在与残余区长度的阶梯式变化（这是理解 residual 机制最直观的方式）。
2. **操作步骤**：
   - 在 [evaluation/llama.py:680](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L680) 附近（`if cur_residual_len == self.residual_block_size:` 之前）加入两行（示例代码，非项目原有）：

     ```python
     if self.layer_idx == 0:
         print(f"[decode] v_pack: {v_pack.shape}, cur_residual_len: {cur_residual_len}")
     ```

   - 注意仓库在第 685-687 行本就留有一段被注释的类似打印，说明作者调试时也用同样的手段。
   - 在有 GPU 且完成安装的机器上，用第 2 讲的环境跑 `python evaluation/example.py`（参数见 `scripts/example.sh`）观察输出。
3. **需要观察的现象**：decode 阶段 `v_pack.shape[1]`（打包后的序列长度）保持不变若干步后突然增加一列（`residual_block_size // pack_nums` 或按 V 布局为 `residual_block_size`），同时 `cur_residual_len` 从 1 递增到 `residual_block_size` 后归 1——呈锯齿/阶梯状。
4. **预期结果**：打印呈现周期性阶梯，周期恰为 `residual_block_size` 步；prefill 阶段（q_len>1 分支）不会触发这两行打印。
5. 无 GPU 环境时，改为静态推导：设初始 `seqlen_k=1000`、`residual_block_size=128`，prefill 后残余区已有 `1000 % 128 = 104` 个 token，写出此后每一步 `cur_residual_len` 的取值序列（104, 105, ..., 128, 1, 2, ...）并标注哪一步触发 `update_pack`（待本地验证运行输出）。

#### 4.3.5 小练习与答案

**练习 1**：`example.py` 里三行猴子补丁（第 9-11 行）如果放到 `from llama import LlamaForCausalLM`（第 14 行）之后，会出什么问题？

**答案**：模块导入顺序上，`llama.py` 第 56 行 `from bit_decode import ... Cache, DynamicCache, StaticCache` 直接从 bit_decode 拿类，本身不受影响；但 transformers 官方代码内部（如 `GenerationMixin`、`LlamaModel.forward` 的缓存构造路径）引用的是 `transformers.cache_utils` 模块命名空间里的名字。若补丁发生在那些模块已绑定旧类之后，`generate()` 内部创建的缓存仍是官方 `DynamicCache`，缺少 `update_pack`/`update_residual` 方法，decode 时会抛 `AttributeError` 或走错路径。因此补丁必须先于一切会使用缓存类的导入执行。

**练习 2**：decode 分支里 `seqlens_k = torch.full((batch_size,), seqlen_pack, ...)`（[llama.py:653](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L653)）传给 C++ 后被赋给 `params.cu_seqlens_k`（[decode_api.cpp:473](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L473)）。这个张量语义是什么？

**答案**：它告诉 kernel「打包主缓存中每个 batch 的有效 KV 长度」。这里每个样本都是满的 `seqlen_pack`（打包块的 token 数），而残余区的实际长度由 `new_lens=cur_residual_len` 单独传递——两个字段分工明确：前者索引打包区，后者索引 FP16 残余区。（注意它与 prefill 打包时用的前缀和数组 `cu_seqlens_k` 同名不同义：前者是「每 batch 长度」，后者是「累加边界」，细节在第 3 单元展开。）

**练习 3**：为什么 prefill 分支先用 FP16 flash-attn 算注意力，再量化打包，而不是量化后用低比特 kernel 算？

**答案**：prefill 的 Q 是整个提示词，注意力计算量占比大且发生在量化之前——此时缓存尚未建立；用成熟的 FP16 kernel 保证提示词阶段精度与吞吐。量化打包只影响**之后**的 decode 读取。这也符合「只加速 memory-bound 的 decode、不动 compute-bound 的 prefill」的设计动机（第 1 讲的带宽分析）。

## 5. 综合实践

**任务：画出 BitDecoding 的全链路模块依赖图（本讲核心实践）。**

1. **实践目标**：把本讲三个模块串成一张分层依赖图，每个节点标注「所在目录 + 语言」，每条边标注「调用的函数/符号」。画完这张图，你就拥有了阅读后续所有单元的「导航图」。

2. **操作步骤**：
   - 准备一张纸或任意画图工具，按 4 层画节点（建议从上到下）：

     | 层 | 节点 | 目录 | 语言 |
     | --- | --- | --- | --- |
     | L4 模型层 | `LlamaBitDecoding.forward`（prefill/decode 两分支） | `evaluation/` | Python |
     | L3 接口层 | `kvcache_pack_int` / `fwd_kvcache_int` | `bit_decode/` | Python |
     | L2 绑定层 | `kvcache_qpack<4>` / `mha_fwd_kvcache<4>` + `PYBIND11_MODULE` | `csrc/bit_decode/` | C++ |
     | L1 kernel 层 | `flash_qpack_kernel` / `flash_fwd_residual_kernel` / `flash_fwd_splitkv_kernel` / `flash_fwd_splitkv_combine_kernel` | `csrc/bit_decode/src/` | CUDA C++ |

   - 连边并标注（两条链共 7 条主要边）：
     - prefill 链：`LlamaBitDecoding(prefill)` → `kvcache_pack_int` → `kvcache_pack_int4`（pybind）→ `kvcache_qpack<4>` → `run_kvcache_qpack_` → `run_flash_qpack` → `flash_qpack_kernel`；
     - decode 链：`LlamaBitDecoding(decode)` → `fwd_kvcache_int` → `fwd_kvcache_int4`（pybind）→ `mha_fwd_kvcache<4>` → `run_mha_fwd_splitkv_dispatch` → `run_flash_splitkv_fwd` → 残余/splitkv/combine 三 kernel。
   - 用 grep 逐条验证边真实存在（每条命令都应在源码中命中）：

     ```bash
     grep -n "kvcache_pack_int\|fwd_kvcache_int" evaluation/llama.py        # L4→L3
     grep -n "kvcache_pack_int4\|fwd_kvcache_int4" bit_decode/bit_decode_interface.py  # L3→L2
     grep -n "PYBIND11_MODULE" -A 6 csrc/bit_decode/decode_api.cpp          # L2 注册表
     grep -n "run_mha_fwd_splitkv_dispatch\|run_kvcache_qpack_" csrc/bit_decode/decode_api.cpp  # L2→L1
     grep -n "kernel<<<\|kernel_res<<<\|flash_fwd_splitkv_combine_kernel<" csrc/bit_decode/src/flash_fwd_launch_template.h  # L1 启动
     ```

3. **需要观察的现象**：每条 grep 都能命中且行号与本讲 4.2/4.3 节引用的行号一致；图中不存在「无源可查」的边。
4. **预期结果**：得到一张 4 层 10 节点、标注了目录/语言/符号的依赖图；对照本讲 4.2.2 节的两段流程伪代码，结构应完全一致。额外的收获：图上能直观看到「一个 Python API 对应 4 个 pybind 函数、对应 4 个 GPU kernel」的扇出关系，以及 `Flash_fwd_params` 结构体横跨 L2 与 L1 的枢纽地位。
5. 若想给图加上「数据流」维度（FP16 输入在哪一层变成 uint16 打包、在哪一层反量化回 FP16），需要第 4、5 单元的知识，可先留白标注「待第 4/5 单元补充」。

## 6. 本讲小结

- 仓库分三层：`bit_decode/`（Python 门面）→ `csrc/bit_decode/`（pybind 绑定 + CUDA kernel）→ `evaluation/`（改造版 HF 模型与评测脚本），语言层级依次为 Python → C++ → CUDA C++。
- Python 侧只有两个核心 API：`kvcache_pack_int`（prefill 量化打包）与 `fwd_kvcache_int`（decode 低比特注意力），内部按 `num_bits` 分流到 `bit_decode_cuda` 的 `int2`/`int4` 四个绑定函数。
- C++ 侧用 `Flash_fwd_params` 结构体承载全部张量指针/stride/量化配置，`decode_api.cpp` 的 `if` 链做运行期到编译期的模板 dispatch，`genfile/*.cu` 提供显式实例化（当前仅启用 k-channel + group_size 128/32）。
- decode 一次调用会依次启动三个 kernel：FP16 残余 kernel、split-KV 打包区 kernel、LSE 合并 kernel；`num_splits` 启发式额外 +1 就是留给残余 kernel 的。
- 模型接入靠三件事：复制 HF modeling 文件新增 `LlamaBitDecoding` 注意力类、扩展版 `DynamicCache`、入口脚本对 `transformers.cache_utils` 的猴子补丁；`config.attn_backend` 经 `LLAMA_ATTENTION_CLASSES` 注册表选择后端。
- `LlamaBitDecoding.forward` 按 `q_len` 分双路径：prefill 用 FP16 flash-attn 算注意力后调用打包链；decode 每步把新 token 追加进补零的残余缓冲，调用解码链，攒满 `residual_block_size` 时回写主缓存并清空残余。

## 7. 下一步学习建议

下一讲（u1-l4「跑通第一个例子」）将实际运行 `evaluation/test.py` 与 `scripts/example.sh`，把本讲的静态地图变成动态体验，观察 32 轮 decode 的误差曲线。之后进入第二单元：

- **u2-l1（量化布局）**：本讲多次出现的 `k_pack`/`k_params` 形状将得到系统解释——k-channel 与 k-tensor 两种模式下 8 个张量的形状推导。
- **u2-l2（residual 机制）**：本讲 4.3.4 实践中观察到的「阶梯现象」的完整原理。
- **u2-l3（Python 接口精读）**：深入 `bit_decode_interface.py` 每个参数的语义。

建议继续阅读的源码（按难度递增）：先通读 [evaluation/test.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py)（160 行，是全链路的最小可运行样本），再浏览 [csrc/bit_decode/src/include/flash.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h) 中的 `Flash_fwd_params` 字段（感受参数结构体的全貌，第 3 单元精读）。

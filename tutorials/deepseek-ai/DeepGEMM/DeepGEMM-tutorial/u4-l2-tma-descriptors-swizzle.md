# TMA 描述符与 swizzle

## 1. 本讲目标

本讲承接 u3-l2（代码生成与模板实例化）和 u4-l1（DeviceRuntime 与运行时配置）。上一讲我们知道：宿主 `Runtime` 类的 `launch_impl` 会在启动 kernel 之前，为 A/B/C-D/SF 这些张量各构造一个「TMA 描述符」（tensor map），然后把它随 kernel 一起送进设备。本讲就回答这中间最关键的一步：

- **TMA 描述符到底是什么、解决了什么问题，它由哪些参数拼出来？**
- **`K-major` 与 `MN-major` 这两种「主维」如何决定张量的内/外维，又如何映射进描述符？**
- **swizzle（32B/64B/128B）是干嘛的？它如何用一次地址重排消除共享内存的 bank conflict？**
- **为什么缩放因子 SFA/SFB 必须是 MN-major、且不做 swizzle？FP4 packed 数据有什么特殊处理？**

学完后，你应该能看懂 `DG_JIT_DEBUG=1` 下打印的 `Making TMA desc: ...` 一行，逐字段说出每个数字的含义，并能解释「为什么 SFA/SFB 要求 MN-major」。

---

## 2. 前置知识

### 2.1 从「逐字节拷贝」到 TMA

在老式 GPU 编程里，把数据从全局内存（global memory，所有 SM 共享的显存）搬进共享内存（shared memory，每个 SM 私有的高速缓存）要靠每个线程发一条 `ld.global` / `st.shared`，靠线程自己去算地址、自己去取数据。这种方式有两个问题：指令多、地址计算繁琐；而且容易产生共享内存的 **bank conflict**（下面解释）。

Hopper（SM90）和 Blackwell（SM100）引入了 **TMA（Tensor Memory Accelerator）**：一块专用的硬件单元，你只要给它一个「描述符」（tensor map / `CUtensorMap`），告诉它「源张量在全局内存里长什么样、每次要拷一个多大的瓦片、目标共享内存用什么布局」，它就能**异步地**把一整块瓦片搬好，期间线程可以去干别的（比如算上一个瓦片）。这就是 DeepGEMM 能做 k-loop 流水线（u6）的前提。

构造描述符的 CUDA Driver API 是 `cuTensorMapEncodeTiled`，DeepGEMM 用 `lazy_cuTensorMapEncodeTiled` 延迟加载它。

### 2.2 共享内存 bank conflict 与 swizzle

共享内存被切成 32 个 **bank**，每个 bank 每周期只能服务一次访问。如果同一个 warp（32 个线程）里有两个线程访问「落在同一个 bank、但不是同一个字」的地址，就会发生 **bank conflict**，访问被串行化、带宽减半。

一个朴素的行主序 `[M, K]` 矩阵放进共享内存时，连续 K 维度的数据会轮流落进 bank 0, 1, 2, …，当 K 是 32 的倍数时，相邻行同列的元素正好落在同一个 bank，访问时极易冲突。

**swizzle（混洗）** 的思路：在写入共享内存时，对地址做一次可逆的 XOR 重排，让本会撞 bank 的地址被「打散」到不同 bank，而读取时硬件按同样的规则还原，**逻辑上等价、物理上无冲突**。TMA 硬件原生支持 `32B / 64B / 128B` 三种 swizzle 原子（atom），由描述符里的一个字段指定。

### 2.3 几个本讲反复出现的量

| 符号 | 含义 |
|---|---|
| `block_m / block_n / block_k` | 一次 GEMM 计算分块在 M/N/K 三个维度的大小 |
| `elem_size` | 单个元素的字节数（FP8=1，BF16=2，FP32=4，FP4 打包后=0.5） |
| `major`（K-major / MN-major） | 某个维度的「主维」，stride 为 1、物理上连续的那一维 |
| `swizzle_mode` | swizzle 原子大小，取 `0/16/32/64/128`（字节） |

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [csrc/jit_kernels/impls/runtime_utils.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp) | **本讲核心**。所有 TMA 描述符构造函数与 major/swizzle 的工具函数都集中在这里 |
| [csrc/utils/math.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/math.hpp) | `ceil_div` 与 `get_tma_aligned_size`（SF 内维对齐工具） |
| [csrc/jit_kernels/heuristics/utils.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/utils.hpp) | `get_swizzle_mode`：根据内维字节数挑 swizzle 原子 |
| [csrc/jit_kernels/heuristics/sm90.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp) | `get_storage_config`：把 block 大小翻译成 swizzle 模式等存储配置 |
| [csrc/jit_kernels/heuristics/config.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp) | `StorageConfig` 结构体，承载 swizzle_*_mode 等字段 |
| [csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp) | 真实调用 `make_tma_*_desc` 的宿主 `launch_impl` 现场 |

---

## 4. 核心概念与源码讲解

### 4.1 TMA 描述符：把全局内存的一块「搬」进共享内存

#### 4.1.1 概念说明

`cuTensorMapEncodeTiled` 的本质是「**为一块全局内存张量建立一份搬运契约**」。它不拷数据，而是把下面这些信息编码进一个 128 字节的 `CUtensorMap` 对象，之后内核里只要 `cp.async.bulk.tensor`（TMA load）就能按契约搬数据：

- **全局内存的形状与步长**：源张量在 global memory 里每一维多大、外维步长多少字节。
- **瓦片盒（box）大小**：每次 TMA 拷贝「一小块」进共享内存，这个盒子的尺寸就是共享内存里那个 stage 的尺寸。
- **元素数据类型**：告诉硬件每个元素几位、如何对齐。
- **swizzle / interleave / L2 promotion / OOB fill**：物理布局与边界处理策略。

DeepGEMM 把这套调用封装在 [make_tma_2d_desc](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L113-L150)（2D，普通 GEMM 的 A/B/CD/SF）和 [make_tma_3d_desc](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L152-L188)（3D，分组/批量场景）两个函数里。两者的参数是对称的，区别只是维度数。

#### 4.1.2 核心流程

以 2D 为例，`make_tma_2d_desc` 的执行流程：

1. 取元素大小 `elem_size`；若开了 swizzle，把**共享内存内维**强制改为 `swizzle_mode / elem_size`（因为 swizzle 原子定义了 smem 一行的逻辑宽度，见 4.3）。
2. 若是 FP4 打包数据，按 `fp4_unpacked_smem` 决定内维约束（见 4.4）。
3. 组装四个数组喂给 `cuTensorMapEncodeTiled`：
   - `gmem_dims[2] = {gmem_inner_dim, gmem_outer_dim}`（全局内存内/外维，**内维在数组下标 0**）
   - `smem_dims[2] = {smem_inner_dim, smem_outer_dim}`（瓦片盒内/外维）
   - `gmem_strides[1] = {gmem_outer_stride * elem_size}`（外维步长，**单位字节**）
   - `elem_strides[2] = {1, 1}`（CUDA 固定要求全 1）
4. 在 `DG_JIT_DEBUG` 开启时打印一行 `Making TMA desc: ...` 便于排错。
5. 调 `lazy_cuTensorMapEncodeTiled`，固定填 `INTERLEAVE_NONE`、`L2_PROMOTION_L2_256B`、`FLOAT_OOB_FILL_NONE`，swizzle 由 `mode_into_tensor_map_swizzle` 翻译。

#### 4.1.3 源码精读

数据类型到 TMA 类型的映射在 [aten_dtype_to_tensor_map_dtype](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L75-L92)：注意 FP8 (`kFloat8_e4m3fn`) 被当作 `UINT8` 传给 TMA（TMA 不关心语义，只看位宽），而 FP4 有 `ALIGN8B` / `ALIGN16B` 两种对齐变体。

真正的契约构造（去掉 FP4 特判后的主干）：

```cpp
// runtime_utils.hpp:133-149
CUtensorMap tensor_map;
const cuuint64_t gmem_dims[2]    = {gmem_inner_dim,  gmem_outer_dim};
const cuuint32_t smem_dims[2]    = {smem_inner_dim,  smem_outer_dim};
const cuuint64_t gmem_strides[1] = {static_cast<cuuint64_t>(gmem_outer_stride * elem_size)};
const cuuint32_t elem_strides[2] = {1, 1};
if (get_env<int>("DG_JIT_DEBUG")) {
    printf("Making TMA desc: global memory: %d %d, shared memory: %d %d, "
           "outer stride: %d, swizzle: %d (base: %d), elem size: %d, ...\n", ...);
}
DG_CUDA_DRIVER_CHECK(lazy_cuTensorMapEncodeTiled(
    &tensor_map, aten_dtype_to_tensor_map_dtype(...), 2, t.data_ptr(),
    gmem_dims, gmem_strides, smem_dims, elem_strides,
    CU_TENSOR_MAP_INTERLEAVE_NONE, mode_into_tensor_map_swizzle(swizzle_mode, swizzle_base),
    CU_TENSOR_MAP_L2_256B, CU_TENSOR_MAP_FLOAT_OOB_FILL_NONE));
```

两个关键约定要记住：

- **`gmem_dims` / `smem_dims` 的下标 0 是内维（unit-stride）**，下标 1 是外维。CUDA 文档里这叫 innermost-first。
- **`gmem_strides` 单位是字节**，所以代码里写的是 `outer_stride * elem_size`，把「元素数」转成「字节数」。

#### 4.1.4 代码实践

1. **目标**：在真实调用里看到一行 TMA 描述符的打印。
2. **步骤**：在一个能 import 成功的环境里运行任意一次 SM90 FP8 GEMM（可参考 u1-l4 的最小调用），并在前面加上 `DG_JIT_DEBUG=1`。
3. **观察**：控制台会打印类似 `Making TMA desc: global memory: 4096 4096, shared memory: 128 128, outer stride: 4096, swizzle: 128 (base: 0), elem size: 1, pointer: ...` 的若干行（A、B、SFA、SFB、CD 各一行）。
4. **预期结果**：一次普通 GEMM 会打印 5 个 2D 描述符；你会注意到 A、B 的 `outer stride` 等于 K，CD 的 swizzle 为 0（FP32 输出不做 swizzle，见 4.3）。
5. 若当前环境无可用 SM90/SM100 GPU，则「待本地验证」。

#### 4.1.5 小练习与答案

**练习**：`gmem_strides` 为什么只给 1 个元素（数组长度为 1），而 `gmem_dims` 给 2 个？

**答案**：对于一个秩为 N 的张量，CUDA 规定 `gmem_strides` 长度为 N-1：最内维的步长隐含为「1 个元素」（由 `elem_strides` 表示），只需要给出每个外维相对再外一维的步长。所以 2D 张量只有 1 个外维步长，写在 `gmem_strides[0]`。

---

### 4.2 major 决定内外维：K-major 与 MN-major

#### 4.2.1 概念说明

回顾 u2-l1：DeepGEMM 的 GEMM 遵循 `D = C + A @ B`，A 是 `[M, K]`、B 是 `[N, K]`（NT 布局下 B 已转置）。一个矩阵在内存里存成 2D，必定有一维 stride 为 1（物理连续），称为 **主维（major）**：

- **K-major**：K 维连续（相邻 K 索引在内存里相邻）。
- **MN-major**：M 或 N 维连续。

这个「谁连续」直接决定了上面 TMA 描述符里哪个维当 `gmem_inner_dim`（内维、下标 0）。在 [get_inner_outer_dims](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L14-L16) 里，规则被写成一句话：

```cpp
// runtime_utils.hpp:14-16
static std::pair<int, int> get_inner_outer_dims(const cute::UMMA::Major& major, const int& k, const int& mn) {
    return major == cute::UMMA::Major::K ? std::make_pair(k, mn) : std::make_pair(mn, k);
}
```

即 **K-major → 内维是 K；MN-major → 内维是 MN**。返回的 `first` 永远是内维、`second` 永远是外维。

为什么这点重要？因为 SM90 的 WGMMA 指令**强制要求操作数为 K-major**（见 u6-l2），所以 u2-l1 里才有「SM90 只支持 NT、且 `fp8_requires_k_major()` 在 arch_major==9 时为真」的结论。SM100 的 UMMA 放宽了这个限制。

#### 4.2.2 核心流程

以 [make_tma_a_desc](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L190-L208)（A 张量描述符）为例，它的逻辑是「两次 `get_inner_outer_dims`」：

1. **全局内存维**：`get_inner_outer_dims(major, shape_k, shape_m * num_groups)` —— 把整张 A 的 K 与「M×分组数」按 major 拆成内/外维。
2. **共享内存维**：`get_inner_outer_dims(major, block_k, block_m)` —— 把每个瓦片盒的 K 与 M 按同一个 major 拆成内/外维。
3. 交给 `make_tma_2d_desc`。

也就是说，**全局内存和共享内存必须用同一套 major**，TMA 搬运才合法。`make_tma_b_desc`、`make_tma_cd_desc` 也是同样的套路，只是把 M 换成 N、把分组数乘到外维。

> 注意 [make_tma_a_desc:198-199](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L198-L199) 有一行硬断言：`if (num_groups > 1) DG_HOST_ASSERT(major == K)`。当 A 带分组（分组 GEMM 的 A），必须 K-major，否则分组维无法整齐地落进外维。

#### 4.2.3 源码精读

来看真实调用现场 [sm90_fp8_gemm_1d1d.hpp:105-121](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L105-L121)：

```cpp
// SM90 上 A、B 恒为 K-major（前面第 86 行已断言）
const auto tensor_map_a  = make_tma_a_desc(major_a, a, m, k,
                                           config.storage_config.load_block_m,
                                           config.layout.block_k, k, 1,
                                           config.storage_config.swizzle_a_mode);
const auto tensor_map_b  = make_tma_b_desc(major_b, b, n, k,
                                           config.storage_config.load_block_n,
                                           config.layout.block_k, k, 1,
                                           config.storage_config.swizzle_b_mode);
const auto tensor_map_sfa = make_tma_sf_desc(cute::UMMA::Major::MN, sfa, m, k, ...);
const auto tensor_map_cd  = make_tma_cd_desc(d, m, n, config.storage_config.store_block_m,
                                             config.storage_config.store_block_n,
                                             static_cast<int>(d.stride(-2)), 1, 0);
```

几个要点：

- 传给 `make_tma_a_desc` 的 `outer_stride` 是 `k`：因为 K-major 下，A 的外维是 M，每换一行（M+1）地址要跨过整行 K 个元素，故步长 = K。`make_tma_2d_desc` 内部会再乘 `elem_size` 变字节。
- **CD 的 major 是写死的**：`make_tma_cd_desc` 直接把 `shape_n` 当内维、`shape_m` 当外维（[make_tma_cd_desc:239-244](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L239-L244)），即输出 D 恒为 N-major（行主序，N 连续），这与 u2-l1「输出 D/C 始终要求行主序」一致。
- **SFA/SFB 的 major 也是写死的 MN**，下一节专门讲。

#### 4.2.4 代码实践

1. **目标**：在源码层面验证「全局内存和共享内存维使用同一套 major」。
2. **步骤**：打开 `runtime_utils.hpp` 的 `make_tma_a_desc`，对照 `get_inner_outer_dims`，分别假设 `major == K` 和 `major == MN`，手算 `(gmem_inner, gmem_outer)` 与 `(smem_inner, smem_outer)`。
3. **观察/预期**：
   - 设 `major=K, block_m=128, block_k=128`：gmem 维 `(k, m)`、smem 维 `(128, 128)`，与 `[M,K]` 矩阵「每行 128 个元素」一致。
   - 设 `major=MN`：内维变成 M，即 `[K,M]` 视角。
4. 这一步是纯阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习**：`make_tma_cd_desc` 为什么不接收 `major` 参数，而是直接写死「内维 = shape_n」？

**答案**：因为输出 D/C 的内存布局在 DeepGEMM 里被约定为恒定的行主序（N 连续），不存在「K-major / MN-major」之分——N 既不是 A/B 的 K，也不参与矩阵乘的归约轴，所以 CD 的内维固定为 N，无需 major 参数。

---

### 4.3 swizzle 模式：用重排消除 bank conflict

#### 4.3.1 概念说明

swizzle 的直觉见 §2.2：把本会撞 bank 的地址用一次可逆 XOR 打散。TMA 硬件支持三种「原子大小」`32B / 64B / 128B`——指 swizzle 重排的基本单位是这么多字节的一小段。原子越大，通常 bank conflict 消除得越彻底、对 tensor core 的喂料越顺，性能越好；但前提是**数据内维的字节宽度必须能被该原子整除**（否则无法对齐重排）。

DeepGEMM 用两个函数配合实现 swizzle 的选择：

- [get_swizzle_mode](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/utils.hpp#L12-L21)：按内维字节数，**从大到小**挑第一个能整除的原子。
- [mode_into_tensor_map_swizzle](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L94-L111)：把整数 mode 翻译成 CUDA 的枚举 `CU_TENSOR_MAP_SWIZZLE_*`。

#### 4.3.2 核心流程

**第一步：选原子**（启发式层）。以 SM90 为例，[get_storage_config](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L132-L139) 决定 A/B/CD 各自的 swizzle：

```cpp
// sm90.hpp:133-139
const auto swizzle_mode_a = get_swizzle_mode(
    desc.major_a == K ? layout.block_k : load_block_m, c10::elementSize(desc.a_dtype));
const auto swizzle_mode_b = get_swizzle_mode(
    desc.major_b == K ? layout.block_k : load_block_n, c10::elementSize(desc.b_dtype));
// FP32 输出不做 swizzle
const auto swizzle_mode_cd = desc.cd_dtype != torch::kFloat ?
    get_swizzle_mode(store_block_n, c10::elementSize(desc.cd_dtype)) : 0;
```

注意挑选依据是**内维的字节数**：K-major 时内维是 `block_k`，否则是 `load_block_m`/`load_block_n`。

`get_swizzle_mode` 的策略是「贪心取最大」：

```cpp
// heuristics/utils.hpp:12-21
static int get_swizzle_mode(const int& block_size, const size_type_t& elem_size) {
    // 16B 其实是非 swizzle（仅 interleave）
    for (const int& mode: {128, 64, 32, 16}) {
        if ((block_size * static_cast<int>(elem_size)) % mode == 0)
            return mode;
    }
    DG_HOST_UNREACHABLE("Unreachable");
}
```

例如 K-major、`block_k=128`、FP8（`elem_size=1`）：内维字节 \(128 \times 1 = 128\)，128 能整除 → 返回 `128`，即 128B swizzle。

**第二步：翻译成枚举**（描述符层）。`mode_into_tensor_map_swizzle` 把上面的整数映射到 CUDA 枚举：

```cpp
// runtime_utils.hpp:94-111
if (base != 0) {                         // 特殊：128B 原子但以 32B 为基底
    DG_HOST_ASSERT(base == 32 and mode == 128);
    return CU_TENSOR_MAP_SWIZZLE_128B_ATOM_32B;
}
DG_HOST_ASSERT(base == 0);
switch (mode) {
    case 0: case 16: return CU_TENSOR_MAP_SWIZZLE_NONE;  // 16 视作无 swizzle
    case 32:         return CU_TENSOR_MAP_SWIZZLE_32B;
    case 64:         return CU_TENSOR_MAP_SWIZZLE_64B;
    case 128:        return CU_TENSOR_MAP_SWIZZLE_128B;
}
```

`base`（第二参数）只有少数特殊布局会用（128B 原子 + 32B 基底），普通 GEMM 走 `base=0` 分支。

**第三步：swizzle 反过来定义 smem 内维**。回到 [make_tma_2d_desc:121-122](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L121-L122)：

```cpp
if (swizzle_mode != 0)
    smem_inner_dim = swizzle_mode / elem_size;
```

为什么开了 swizzle 就要重写 smem 内维？因为 swizzle 原子规定「共享内存每一行逻辑宽度 = 原子字节数」，所以一行能放 `swizzle_mode / elem_size` 个元素。TMA 每次拷的就是这么宽的一「条」，剩下的外维由多次 TMA 拷贝堆叠。

#### 4.3.3 源码精读：TMA 切分与「不切分」约束

当瓦片盒的内维大于 swizzle 原子宽度时，一个完整的 block 需要多次 TMA 拷贝才能搬完——这叫 **TMA split**。[make_tma_cd_desc:237-238](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L237-L238) 的注释点明了这点：

> Swizzling requires the inner box dim to be less or equal than (the swizzle atom) bytes, so `BLOCK_N * sizeof(T) / atom` TMA stores are required.

但 DeepGEMM 的 1D1D kernel 对 A/B **要求不切分**，这在调用现场用断言锁死 [sm90_fp8_gemm_1d1d.hpp:102-103](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L102-L103)：

```cpp
// Requires no TMA splits
DG_HOST_ASSERT(config.storage_config.swizzle_a_mode == config.layout.block_k);
DG_HOST_ASSERT(config.storage_config.swizzle_b_mode == config.layout.block_k);
```

这条断言的含义是：A/B 的 swizzle 原子（字节）必须正好等于 `block_k`（因为 FP8 下 `block_k` 个 1 字节元素 = `block_k` 字节）。这样 `smem_inner_dim = swizzle_mode / 1 = block_k`，一次 TMA 就搬完一整块 K，不需要切分——1D1D kernel 的流水线设计正是建立在「每个 stage 整块到齐」之上。

而 CD 的 swizzle 在 SM90 上**对 FP32 输出直接置 0**（`mode_into_tensor_map_swizzle(0,0)` → `NONE`，见 `get_storage_config` 第 138 行的三目），因为 FP32 输出的 epilogue 用单 warp-group store，走朴素布局即可，swizzle 反而碍事。SM100 的 BF16/FP8 输出则会开启 CD swizzle（见 [sm100.hpp:166-167](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L166-L167)）。

#### 4.3.4 代码实践

1. **目标**：理解 swizzle 原子选择与 TMA 切分的关系。
2. **步骤**：手算几个例子（设 FP8，`elem_size=1`）：
   - `block_k=128`：内维 128 字节 → `get_swizzle_mode` 返回 128 → `smem_inner_dim=128` → `swizzle_a_mode == block_k` ✓ 不切分。
   - 假设 `block_k=64`：返回 64 → `smem_inner_dim=64` → 也等于 block_k ✓ 不切分。
   - 假设某 CD 内维 `block_n=256`、BF16(`elem_size=2`)：内维 512 字节，返回 128 → `smem_inner_dim=128` 元素 ≠ 256 → 需要 \(256/128=2\) 次 TMA store（切分）。
3. **预期结果**：你能用「内维字节 ÷ 返回的原子」判断是否切分，以及切几次。
4. 可选：在 `DG_JIT_DEBUG=1` 下核对打印里 A 的 `swizzle` 值是否等于 `shared memory` 的内维。

#### 4.3.5 小练习与答案

**练习**：为什么 `get_swizzle_mode` 要从大到小（128→64→32→16）遍历，而不是取最小？

**答案**：因为更大的 swizzle 原子对 bank conflict 的消除更彻底、对 tensor core 喂料更友好，性能通常更好。只要内维字节能被大原子整除（能对齐重排），就应优先选大原子。SM90 启发式甚至额外要求 swizzle 至少 64B（[sm90.hpp:101-102](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L101-L102) 注释「32B 的性能较差」），直接淘汰只匹配到 32B 的候选布局。

---

### 4.4 缩放因子(SF)与 FP4 packed 的特殊处理

#### 4.4.1 概念说明

回顾 u2-l2：FP8/FP4 范围窄，需要**逐块缩放因子（SF）**，粒度由 `recipe=(gran_mn, gran_k)` 描述。SF 本身也是一个二维网格——沿 MN 每 `gran_mn` 一个、沿 K 每 `gran_k` 一个。把 SF 也喂给 tensor core，需要给它单独构造一个 TMA 描述符，这就是 [make_tma_sf_desc](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L247-L267)。

SF 描述符有两条与 A/B/CD 截然不同的硬约束：

1. **必须 MN-major**（不管 A/B 本身是 K-major 还是 MN-major）。
2. **不做 swizzle**（`swizzle_mode == 0`）。

#### 4.4.2 核心流程：为什么 SF 要 MN-major 且不 swizzle

先看代码里这两条约束如何被强制：

```cpp
// runtime_utils.hpp:255-258
DG_HOST_ASSERT(major == cute::UMMA::Major::MN);
// TODO: maybe swizzle SF as well
DG_HOST_ASSERT(swizzle_mode == 0);
```

调用现场也把 major 写死成 MN（[sm90_fp8_gemm_1d1d.hpp:113](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L113) `make_tma_sf_desc(cute::UMMA::Major::MN, sfa, m, k, ...)`）。

为什么是 MN-major？可从 SF 的结构与消费方式推导：

- **结构上**：SF 网格在 K 轴上很稀疏（每 `gran_k=128` 才一个），在 MN 轴上较密（每 `gran_mn` 一个）。把较密的 MN 放成内维（unit-stride、连续），让「同一个 K 段下、不同输出 tile 的 SF」在内存里连续排列，TMA 一次就能搬出一整条 MN 方向的 SF。
- **消费上**：tensor core 在沿 K 归约时，是**按 MN 子 tile**应用缩放因子的——每个输出（MN）子块对应一行 SF。MN-major 让「SF 行」与「输出子 tile」在地址上平行，索引最简单。无论 SM90 的 WGMMA（SF 作为 FP32 与累加器做乘加）还是 SM100 的 UMMA（专用 SF 描述符，见 u6-l2），都按这种「MN 连续」的约定读取 SF。
- **对齐**：`shape_mn` 经 [get_tma_aligned_size](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/math.hpp#L23-L27) 向上对齐到 16 字节边界，保证 TMA 内维满足 16B 对齐要求；不 swizzle 是因为 SF 数据量小、bank conflict 不是瓶颈（注释 `TODO: maybe swizzle SF as well` 也表明作者认为目前无需）。

#### 4.4.3 源码精读

SF 外维的计算体现了 FP32（SM90）与打包 UE8M0（SM100）的差异 [runtime_utils.hpp:260-266](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L260-L266)：

```cpp
shape_mn = get_tma_aligned_size(shape_mn, static_cast<int>(t.element_size()));
return make_tma_2d_desc(t,
    shape_mn,                                                  // 内维 = MN（对齐后）
    ceil_div(shape_k, gran_k * (t.scalar_type() == torch::kFloat ? 1 : 4)) * num_groups,  // 外维 = K(×分组)
    block_mn, smem_outer_dim,                                  // smem 盒 = [block_mn, smem_outer_dim]
    shape_mn,                                                  // 外维步长 = shape_mn（MN-major 下跨一行 K）
    swizzle_mode, swizzle_base, allow_tf32);
```

关键在三目 `(kFloat ? 1 : 4)`：

- SM90 用 **FP32** 存 SF，每个 SF 一个 float，K 方向 tile 数 = `ceil_div(shape_k, gran_k)`，因子为 1。
- SM100 用**打包 UE8M0**（4 个 8 位指数打包进一个 int32，见 u2-l2），所以 K 方向「元素数」要再除 4，因子为 4。这与 u2-l2「SM100 SF 形状 K 维再除 4」一致。

**FP4 packed 的特殊处理**回到 `make_tma_2d_desc` 的两处 [runtime_utils.hpp:124-131](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L124-L131)：

```cpp
if (t.scalar_type() == kPackedFP4) {
    // 内维必须是 64B 的倍数（为 .b4x16_p64 指令）
    DG_HOST_ASSERT(not fp4_unpacked_smem or gmem_inner_dim % 128 == 0);
    // packed smem 模式下，smem 内维 = swizzle_mode * 2
    if (not fp4_unpacked_smem and swizzle_mode != 0)
        smem_inner_dim = swizzle_mode * 2;
}
```

FP4（e2m1）两位一个元素，两个元素打包成 1 字节。这里有两个分支：

- **unpacked smem（默认 `fp4_unpacked_smem=true`）**：TMA 在搬运时把 packed FP4 **解包**成更宽的类型（`16U4_ALIGN16B`），全局内存内维必须是 128（元素）的倍数以对齐 64 字节的 `.b4x16_p64` 解包指令。
- **packed smem（`fp4_unpacked_smem=false`）**：TMA 保持打包形态进 smem（`16U4_ALIGN8B`），此时 `smem_inner_dim = swizzle_mode * 2`——因为每个字节含 2 个 FP4 元素，原子宽度 `swizzle_mode` 字节对应 `swizzle_mode*2` 个 FP4 元素。

数据类型的两种对齐变体见 [aten_dtype_to_tensor_map_dtype:86-89](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L86-L89)。FP4 路径只在 SM100 出现（`#if CUDA_VERSION >= 12080`）。

#### 4.4.4 代码实践

1. **目标**：亲手验证 SF 描述符的「MN-major + 不 swizzle + 外维步长=shape_mn」三件事。
2. **步骤**：
   - 构造一个 `[M=256, K=512]` 的 FP8 张量，`gran_k=128`。
   - 手算 SM90（FP32 SF）下：内维 `shape_mn`（经 16B 对齐），外维 `ceil_div(512, 128*1)=4`，外维步长 `= shape_mn`。
   - 手算 SM100（UE8M0 打包，element_size 视为 1 字节、4 个打包）下：外维 `ceil_div(512, 128*4)=1`。
3. **观察/预期**：两种架构 SF 外维不同（4 vs 1），印证 u2-l2「SM100 SF 在 K 维再除 4」。
4. 若想看真实值：在 `DG_JIT_DEBUG=1` 下找到打印 SF 那一行的 `swizzle: 0`，验证 SF 确实不 swizzle。

#### 4.4.5 小练习与答案

**练习 1**：SF 描述符为什么断言 `swizzle_mode == 0`，而 A/B 的 swizzle 通常是 128？

**答案**：SF 数据量很小（一个输出 tile 才一两个 SF），共享内存 bank 远未被 SF 压满，bank conflict 不是瓶颈；且 SF 的消费方式（按 MN 子 tile 行读取）朴素行主序就够用。A/B 是大批量主数据，喂 tensor core 时 bank conflict 是真瓶颈，必须用大 swizzle 原子消除。

**练习 2**：FP4 unpacked smem 模式下，为什么全局内存内维要 `% 128 == 0`？

**答案**：unpacked 模式下 TMA 用 `.b4x16_p64` 类指令把 packed FP4 解包，该指令一次处理 64 字节（=128 个 FP4 元素，每个 4 位），故全局内存内维必须是 128 个 FP4 元素的倍数才能对齐。

---

## 5. 综合实践

把本讲三块知识串起来：**用源码追踪一次 SM90 FP8 GEMM 的全部 5 个 TMA 描述符是怎么构造出来的**。

任务步骤：

1. 从 [sm90_fp8_gemm_1d1d.hpp:99](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L99) 的 `get_best_config` 出发，假设拿到的配置是 `block_m=128, block_n=128, block_k=128`，FP8 输入、FP32 输出。
2. 填写下面这张表（逐项推导，不要猜）：

   | 描述符 | major | gmem 内维/外维 | smem 内维/外维 | swizzle | 是否 TMA 切分 |
   |---|---|---|---|---|---|
   | A | K | ? | ? | ? | ? |
   | B | K | ? | ? | ? | ? |
   | SFA | MN | ? | ? | 0 | — |
   | SFB | MN | ? | ? | 0 | — |
   | CD | N(写死) | ? | ? | 0 | — |

3. 对照 [runtime_utils.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp) 的各 `make_tma_*_desc` 与 `get_swizzle_mode`，核对你的答案。
4. 如果环境允许，用 `DG_JIT_DEBUG=1` 实跑一次，把打印的 5 行 `Making TMA desc` 与你的表格逐项对齐。

**参考答案要点**（设 `m=n=k` 较大、`gran_k=128`）：

- A：gmem 内/外维 = `(k, m)`，smem = `(128, 128)`，swizzle = 128（128B），不切分。
- B：gmem = `(k, n)`，smem = `(128, 128)`，swizzle = 128，不切分。
- SFA：gmem 内维 = 对齐后的 `m`，外维 = `ceil_div(k,128)`，smem = `(128, 1)`，swizzle = 0。
- CD：gmem = `(n, m)`，smem = `(store_block_n, 64)`（1D1D 的 store_block_m=warp-group=64），FP32 输出 swizzle = 0。

---

## 6. 本讲小结

- TMA 描述符（`CUtensorMap`）是一份「搬运契约」，由 `cuTensorMapEncodeTiled` 编码全局内存形状/步长、瓦片盒大小、数据类型、swizzle 等；DeepGEMM 把它封装在 `make_tma_2d_desc` / `make_tma_3d_desc`。
- `K-major` 把 K 当内维、`MN-major` 把 MN 当内维，规则就是 [get_inner_outer_dims](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L14-L16) 那一行；SM90 强制 K-major，SM100 放宽。
- swizzle 用可逆 XOR 重排消除共享内存 bank conflict，原子越大越好；`get_swizzle_mode` 贪心取最大可整除原子，开了 swizzle 后 smem 内维被重写为 `swizzle_mode / elem_size`。1D1D kernel 用断言锁死 A/B 不做 TMA 切分。
- 缩放因子 SFA/SFB **必须 MN-major 且不 swizzle**，外维步长 = 对齐后的 `shape_mn`；FP32（SM90）与打包 UE8M0（SM100）在外维计算上差一个「÷4」。
- FP4 packed 有 unpacked / packed 两种 smem 模式，分别对应 `ALIGN16B` / `ALIGN8B` 数据类型与不同的内维对齐约束，仅在 SM100 出现。
- 所有这些参数都能在 `DG_JIT_DEBUG=1` 的 `Making TMA desc` 打印里逐一核对。

---

## 7. 下一步学习建议

本讲讲清了「描述符怎么构造」，但还没讲「描述符造好后，设备 kernel 怎么用它异步搬数据、怎么与 mbarrier 配合做流水线」。建议：

- 进入 **u6-l1（内核入口：SM90 FP8 GEMM 1D1D）**：看 `__launch_bounds__`、共享内存各 stage 划分、TMA 线程与 math 线程的分工，以及 k-loop 流水线如何把本讲的 TMA 描述符真正用起来。
- 进入 **u6-l2（MMA 抽象：WGMMA vs UMMA）**：理解 SM90 WGMMA / SM100 UMMA 如何消费本讲构造的 swizzled 共享内存，以及 SM100 的 SF descriptor 如何对应本讲的 MN-major SF。
- 进入 **u6-l3（PTX 内联函数：TMA 加载与栅栏）**：看 `cp.async.bulk.tensor` 这类 PTX 指令如何与本讲的 `CUtensorMap` 配合，完成异步拷贝与 mbarrier 同步。

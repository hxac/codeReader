# TASSIGN 与片上内存规划

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 TASSIGN 在 manual 模式下做了什么：把一个**只有编译期形状信息、不拥有内存**的 Tile 对象，绑定到 UB/L0 等片上缓冲中一个**具体的字节偏移地址**。
2. 查表得出各平台（A2A3/A5/A6/Kirin 系列）UB、L1、L0A/L0B/L0C 等片上缓冲的容量与 32 字节对齐约束，并能据此为给定 tile 尺寸计算 UB 用量。
3. 读懂 `demos/baseline/add` 中手工规划的 ping-pong（乒乓）UB 地址表，并能独立设计一张自己的地址分配表。
4. 用 `static_assert` 在编译期拦截"地址表超出 UB 容量"这类错误，并理解 PTO 内建的 `TASSIGN<Addr>(tile)` 编译期检查（SA-0351~SA-0354）在哪些后端生效、哪些后端是空操作。

本讲承接 u2-l3 的结论——"Tile 自身不拥有内存，地址绑定由 TASSIGN 完成"，把这块拼图补上。

## 2. 前置知识

- **片上缓冲层级**（u1-l4 已接触）：NPU 一个核内有 UB（Unified Buffer，向量流水线用）、L1（Mat）、L0A/L0B（Cube 左/右矩阵）、L0C（累加）等多块物理上独立的 SRAM。数据要先从 GM（Global Memory）搬进片上，计算完再搬回去。
- **TileType 决定物理位置**（u2-l3）：`TileType::Vec` 的 tile 住在 UB，`Left` 住 L0A，`Right` 住 L0B，`Acc` 住 L0C。Tile 模板的第一个参数就选好了"房间"。
- **manual 模式 vs Auto 模式**：manual 模式下片上内存完全由程序员手工划分——每个 tile 放在哪个偏移、留多大空间、怎么排乒乓，都写在代码里；Auto 模式则由编译器代劳（u7-l1 会专门讲）。本讲全部针对 manual 模式。
- **一个类比**：Tile 类型像一张"房间登记单"（几行几列、什么布局、有效区域多大），TASSIGN 就是把登记单钉到房间里某个具体床位上。床位编号以**字节**为单位，从该缓冲的起始地址 0 开始计数。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [include/pto/common/pto_instr.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_instr.hpp) | TASSIGN 的公共 API 声明：运行期重载与编译期地址重载 |
| [include/pto/cpu/TAssign.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TAssign.hpp) | CPU 模拟器后端的 TASSIGN 实现（把偏移映射进模拟内存） |
| [include/pto/npu/a2a3/TAssign.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/npu/a2a3/TAssign.hpp) | NPU 后端的 TASSIGN 实现（整数 → 带地址空间限定的指针） |
| [include/pto/common/pto_tile.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp) | Tile 定义；`data_` 成员与 `TileDType` 在三种模式下的形态 |
| [include/pto/common/buffer_limits.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/buffer_limits.hpp) | 各平台片上缓冲容量与对齐常量（本讲第二主角） |
| [include/pto/common/tassign_check.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/tassign_check.hpp) | `TASSIGN<Addr>(tile)` 的编译期检查（SA-0351~SA-0354） |
| [include/pto/common/constants.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/constants.hpp) | `TMP_UB_SIZE`/`TMP_UB_OFFSET`：UB 尾部 PTO 自留临时区 |
| [demos/baseline/add/csrc/kernel/add_custom.cpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp) | 手工 UB 地址规划 + 乒乓缓冲的完整样板（板端 demo） |
| [tests/cpu/st/testcase/tadd/tadd_kernel.cpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp) | 最简 TASSIGN 用法（三个 tile 各绑一个地址） |
| [include/pto/cpu/NPUMemoryModel.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/NPUMemoryModel.hpp) | CPU 模拟器如何模拟 UB/L1/L0 地址空间 |

## 4. 核心概念与源码讲解

### 4.1 TASSIGN 手动布局：把 Tile 绑到片上地址

#### 4.1.1 概念说明

u2-l3 精读 Tile 模板时我们说过：`Tile` 只封装了容量形状、布局、有效区域这些**编译期信息**，它的 `data_` 成员在没有绑定地址前是空的。manual 模式下，把"登记单"钉到"床位"上的动作就是：

```cpp
TASSIGN(tile, 0x4000);   // tile 的数据将存放在该缓冲偏移 0x4000 处
```

三个关键语义：

1. **地址是"实现相关地址"**：一个无符号整数，单位是字节，表示**相对本缓冲起始位置**的偏移。Tile 的 `TileType` 决定它是 UB 的偏移还是 L0A 的偏移——两个缓冲各自从 0 编号，互不相干。
2. **一词两用**（承接 u2-l2）：Tile 收**整数偏移**；GlobalTensor 收**同类型指针**，用来整体平移 GM 窗口。本讲只讲前者。
3. **不分配、只绑定**：TASSIGN 不检查两块 tile 是否重叠、不管理生命周期。地址表的对错完全由程序员负责——这正是 PTO"不隐藏底层能力"的体现。

#### 4.1.2 核心流程

```text
TASSIGN(tile, addr) 的执行路径
─────────────────────────────
公共 API  pto_instr.hpp: TASSIGN(obj, addr)
            └─ MAP_INSTR_IMPL → 后端 TASSIGN_IMPL(obj, addr)
                  ├─ NPU (a2a3/a5):
                  │     整数 addr --reinterpret_cast--> __ubuf__ T* / __l0a__ T* 等限定指针
                  │     写入 tile.data_
                  └─ CPU 模拟器:
                        addr --NPUMemoryModel::ResolveAssignedAddress--> 模拟缓冲内的宿主指针
                        写入 tile.data_
```

之后所有消费这个 tile 的指令（TLOAD/TADD/TSTORE…）都通过 `tile.data()` 拿到这个地址去读写。

#### 4.1.3 源码精读

公共 API 有两个重载，[include/pto/common/pto_instr.hpp:101-L118](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_instr.hpp#L101-L118)(https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_instr.hpp#L101-L118)：运行期版本 `TASSIGN(obj, addr)` 直接分发到后端；编译期地址版本 `TASSIGN<Addr>(obj)` 把地址作为模板参数传入，先触发 `tassign_static_check` 的静态检查（见 4.4 节），再委托运行期路径。注意后者只对 Tile/ConvTile 启用（`is_tile_data_v || is_conv_tile_v`），GlobalTensor 不适用。

NPU 后端实现非常短，[include/pto/npu/a2a3/TAssign.hpp:17-L35](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/npu/a2a3/TAssign.hpp#L17-L35)：

```cpp
if constexpr (is_tile_data_v<T> || is_conv_tile_v<T>) {
#ifndef __PTO_AUTO__
    static_assert(std::is_integral_v<AddrType>, "Tile can only be assigned with address of int type.");
    obj.assignData(reinterpret_cast<typename T::TileDType>(static_cast<std::uintptr_t>(addr)));
#else
    return;   // Auto 模式下 TASSIGN 是空操作，地址由编译器分配
#endif
}
```

这段代码做了三件事：静态断言地址必须是整数类型；把整数直接重解释成 `TileDType` 类型的指针；Auto 模式下干脆什么都不做。`TileDType` 是什么？看 [include/pto/common/pto_tile.hpp:1540-L1555](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1540-L1555)：

```cpp
#if defined(__CPU_SIM) || defined(__COSTMODEL)
    // CPU Sim: data_ is a pointer that TASSIGN can redirect to shared NPU memory
    using TileDType = Tile::DType*;
#else
    // NPU manual 模式：带地址空间限定的指针，如 __ubuf__ T*
    using TileDType = typename MemoryQualifier<Loc, DType>::type;
#endif
```

NPU 上 `MemoryQualifier<TileType::Vec, T>::type` 展开为 `__ubuf__ T*`（见 [include/pto/common/memory.hpp:26-L33](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/memory.hpp#L26-L33)）——CCE 编译器的地址空间限定符，硬件据此访问 UB 而不是通用内存。TileType 到物理缓冲的完整对应关系由 [include/pto/common/type.hpp:123-L134](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/type.hpp#L123-L134) 的枚举值给出：`Vec/Mat/Left/Right/Acc/Bias/Scaling/ScaleLeft/ScaleRight`。

CPU 模拟器的实现则把偏移"翻译"进模拟内存，[include/pto/cpu/TAssign.hpp:22-L37](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TAssign.hpp#L22-L37) 调用 `NPUMemoryModel::Instance().ResolveAssignedAddress<T>(addr)`。`NPUMemoryModel` 按 TileType 把偏移路由到对应模拟区域，[include/pto/cpu/NPUMemoryModel.hpp:208-L218](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/NPUMemoryModel.hpp#L208-L218)：

```cpp
template <typename TileDef>
typename TileDef::DType* ResolveAssignedAddress(std::uintptr_t addr)
{
    if (auto* direct = TryResolveExistingPointer<typename TileDef::DType>(addr)) {
        return direct;      // 已是区域内的宿主指针，直接复用
    }
    return GetPointer<TileDef>(static_cast<std::size_t>(addr));  // 字节偏移 → 区域内指针
}
```

区域路由逻辑在 [include/pto/cpu/NPUMemoryModel.hpp:185-L200](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/NPUMemoryModel.hpp#L185-L200)：`Vec → UB、Mat → L1、Left → L0A、Right → L0B、Acc → L0C`。文件头注释 [include/pto/cpu/NPUMemoryModel.hpp:11-L27](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/NPUMemoryModel.hpp#L11-L27) 对这套映射有完整说明，且每个线程持有独立实例（`thread_local`），模拟"每个核各有自己的 UB/L0"。

最简用例是 tadd 内核，[tests/cpu/st/testcase/tadd/tadd_kernel.cpp:22-L28](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L22-L28)：

```cpp
using TileData = Tile<TileType::Vec, T, kTRows_, kTCols_, BLayout::RowMajor, -1, -1>;
TileData src0Tile(kTRows_, kTCols_);
TileData src1Tile(kTRows_, kTCols_);
TileData dstTile(kTRows_, kTCols_);
TASSIGN(src0Tile, 0x0);
TASSIGN(src1Tile, 0x4000);
TASSIGN(dstTile, 0x8000);
```

三个 tile 依次绑到 UB 偏移 0x0、0x4000、0x8000。这张表为什么是 0x4000 一档？我们算一下：float 用例是 64×64，单个 tile 字节数

\[ 64 \times 64 \times 4 = 16384 = 0\mathrm{x}4000 \]

所以 0x0 / 0x4000 / 0x8000 正是**三块背靠背无缝排布**，总共 0xC000（48KB）。而 half 用例是 16×256，只需 16×256×2 = 0x2000——地址表按最大的实例化（float/int32 的 64×64）预留，小类型只占一半，后面空着也不影响正确性。这就是手工地址规划的最典型样式。

#### 4.1.4 代码实践

**实践目标**：验证 TASSIGN 地址语义——两块 tile 绑到**重叠**地址会发生什么。

**操作步骤**：

1. 复制 `tests/cpu/st/testcase/tadd/` 四件套到同级的 `tadd_overlap/` 目录（`CMakeLists.txt` 里改一行 `pto_cpu_sim_st(tadd_overlap)`，`main.cpp` 的 TEST_F 相应改名，u1-l4 讲过这套"三处同步"约定）。
2. 在内核里加一个第四个 tile 并故意重叠：`TileData dupTile(kTRows_, kTCols_); TASSIGN(dupTile, 0x0);`——与 `src0Tile` 同地址。
3. `TLOAD` 之后调用 `TADD(dupTile, src1Tile, src0Tile)`，再 TSTORE `dupTile` 到输出。

**需要观察的现象**：dupTile 与 src0Tile 共用同一段 UB 内存，TADD 的结果会写穿 src0Tile 的数据；如果后续再消费 src0Tile，读到的已是"和"而不是原始输入。

**预期结果**：CPU 模拟器不会报任何错——TASSIGN 不查重叠（NPU 上同样不查）。输出 golden 比对是否通过取决于你 TSTORE 的顺序。这印证了"地址表对错完全由程序员负责"。具体输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`TASSIGN(tile, 0x4000)` 中 0x4000 是相对谁的偏移？一个 `TileType::Vec` tile 和一个 `TileType::Acc` tile 都绑 0x0，会冲突吗？

**答案**：相对**该 TileType 对应缓冲**（Vec→UB，Acc→L0C）自身的起始地址。不会冲突——两者是物理上不同的存储，各自从 0 编号，CPU 模拟器里也路由到不同模拟区域。

**练习 2**：为什么 CPU 模拟器上 `TileDType` 是裸指针 `DType*`，而 NPU 上是 `__ubuf__ T*` 这样的限定指针？

**答案**：CPU 模拟器跑在本机 g++/clang++ 上，没有 CCE 的地址空间扩展，UB/L0 都只是 `std::vector` 模拟缓冲（`NPUMemoryModel`），所以 `data_` 存宿主指针即可；NPU 上限定指针告诉硬件访问对应的物理 SRAM，编译器也会据此生成正确的访存指令。

**练习 3**：u2-l2 讲过 `TASSIGN(globalTensor, ptr)`，它与本讲的 `TASSIGN(tile, offset)` 在类型检查上有何不同？

**答案**：GlobalTensor 版本要求参数是**同元素类型的指针**（`std::is_pointer_v` + `std::is_same_v` 两层 static_assert，见 [include/pto/npu/a2a3/TAssign.hpp:29-L34](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/npu/a2a3/TAssign.hpp#L29-L34)），语义是平移 GM 窗口；Tile 版本要求**整数**，语义是绑定片上偏移。同一个 API 名字，靠重载按操作数类型分流。

### 4.2 UB 容量规划：buffer_limits.hpp 的容量约束

#### 4.2.1 概念说明

手工规划地址表之前必须先回答：**这个房间里到底有多少床位？**`include/pto/common/buffer_limits.hpp` 就是各平台片上缓冲的"户口本"。它为每块缓冲定义一对宏：

- `PTO_xxx_SIZE_BYTES`：容量，单位字节，按平台架构宏自动推导；
- `PTO_xxx_ALIGN_BYTES`：对齐要求，一律 32 字节。

文件头注释 [include/pto/common/buffer_limits.hpp:16-L24](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/buffer_limits.hpp#L16-L24) 特别说明：每个宏都可以被构建系统用 `-D` 覆盖，以适配非标准配置。这意味着容量约束是"默认值可协商"的，但硬件物理容量不会因此改变——覆盖只用于特殊形态（如仿真配置）。

#### 4.2.2 核心流程

UB 用量的基本公式（Vec tile、常规布局）：

\[ \text{tileBytes} = \text{Rows} \times \text{Cols} \times \text{sizeof}(T) \]

\[ \text{totalBytes} = \sum_{i} \text{tileBytes}_i \times \text{BUFFER\_NUM}_i \]

规划一张地址表要过的四道关：

1. **容量关**：`totalBytes ≤ PTO_UBUF_SIZE_BYTES`；
2. **保留区关**：不能压占 PTO 自留临时区（见下）；
3. **对齐关**：每个起始地址 `% 32 == 0`（tile 尺寸本身还要满足 32 字节行对齐的 static_assert，u2-l3 已讲）;
4. **乒乓关**：交替使用的缓冲之间不能重叠（4.3 节）。

各平台容量速查（数据全部来自 [include/pto/common/buffer_limits.hpp:27-L199](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/buffer_limits.hpp#L27-L199)）：

| 缓冲（TileType） | 宏 | A2A3 | A5/A6 | Kirin9030 | KirinX90 | 对齐 |
|---|---|---|---|---|---|---|
| UB（Vec） | `PTO_UBUF_SIZE_BYTES` | 192KB | 256KB | 128KB | 128KB | 32B |
| L1（Mat） | `PTO_CBUF_SIZE_BYTES` | 512KB | 512KB | 512KB | 1024KB | 32B |
| L0A（Left） | `PTO_L0A_SIZE_BYTES` | 64KB | 64KB | 32KB | 64KB | 32B |
| L0B（Right） | `PTO_L0B_SIZE_BYTES` | 64KB | 64KB | 32KB | 64KB | 32B |
| L0C（Acc） | `PTO_L0C_SIZE_BYTES` | 128KB | 256KB | 64KB | 128KB | 32B |
| Bias | `PTO_BIAS_SIZE_BYTES` | 1KB | 4KB | 1KB | 1KB | 32B |
| FBuffer（Scaling） | `PTO_FBUF_SIZE_BYTES` | 2KB | 4KB | 7KB | 6KB | 32B |
| ScaleLeft/Right | `PTO_SCALELEFT/RIGHT_SIZE_BYTES` | 0（不存在） | 各 4KB | 0 | 0 | 32B |

（KirinDev0000 另有取值，表中略；UB 宏定义见 [L31-L44](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/buffer_limits.hpp#L31-L44)，L1 见 [L51-L64](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/buffer_limits.hpp#L51-L64)，L0A/L0B/L0C 见 [L71-L126](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/buffer_limits.hpp#L71-L126)。）

两个容易踩的坑：

- **ScaleLeft/ScaleRight 在 A2A3 上容量为 0**（[L177-L199](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/buffer_limits.hpp#L177-L199)）——MX scale tile 是 A5 起才有的硬件，在 A2A3 上 TASSIGN 它会直接触发编译期报错（SA-0351，见 4.4 节）。
- **UB 尾部有 PTO 自留临时区**：[include/pto/common/constants.hpp:29-L30](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/constants.hpp#L29-L30) 定义 `TMP_UB_SIZE = 8KB`、`TMP_UB_OFFSET = 184KB`——A2A3 的 192KB UB 中，**从 0x2E000 到 0x30000 这 8KB 是 PTO 部分指令（如 a2a3 后端的 TExtract、TRowExpandBinOp、TCvt 的中间暂存）的固定工作区**。你的地址表不要压进去。仓库示例对这点很谨慎，比如 add_custom 的地址表留了 0x100 的 guard 余量（4.3 节）。

#### 4.2.3 源码精读

UB 宏的定义方式，[include/pto/common/buffer_limits.hpp:27-L44](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/buffer_limits.hpp#L27-L44)：

```cpp
#ifndef PTO_UBUF_ALIGN_BYTES
#define PTO_UBUF_ALIGN_BYTES 32u
#endif

#ifndef PTO_UBUF_SIZE_BYTES
#if defined(PTO_NPU_ARCH_A5)
#define PTO_UBUF_SIZE_BYTES (256u * 1024u)
...
#elif defined(PTO_NPU_ARCH_A2A3)
#define PTO_UBUF_SIZE_BYTES (192u * 1024u)   // 0x30000，add_custom 的 UB_SIZE 就是它
```

三层结构：`#ifndef` 允许外部覆盖 → 架构宏二选一 → 都没有则 `#error` 拒绝编译。所有缓冲宏都是这个模式，认准一个就全会读了。

CPU 模拟器侧的容量由 `NPUMemoryModel` 独立维护，[include/pto/cpu/NPUMemoryModel.hpp:67-L87](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/NPUMemoryModel.hpp#L67-L87) 给出 A2A3（UB 192KB、L1 512KB、L0A/L0B 各 64KB、L0C 128KB）与 A5（UB 256KB、L0C 256KB）两组数值，与 buffer_limits 保持一致。但注意 [L62](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/NPUMemoryModel.hpp#L62) 和 [L103-L111](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/NPUMemoryModel.hpp#L103-L111)：UB 容量被强制抬到**至少 512KB**（`kDefaultCpuSimUBScratchSize`），并可用环境变量 `PTO_CPU_SIM_UB_BYTES` 等继续覆盖。**结论：CPU 模拟器故意不模拟真实的 UB 紧缺**——地址表超了 192KB 在 CPU 上照样能跑，到 NPU 上才会炸。容量纪律必须靠编译期断言（4.4 节）来守。

另外，Tile 自带两个只在 CPU 模拟器下可用的尺寸查询函数，[include/pto/common/pto_tile.hpp:1666-L1677](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1666-L1677)：

```cpp
static constexpr size_t GetSizeInUnits() { ... Numel（twin 类型除以 2）... }
static constexpr size_t GetSizeInBytes() { return GetSizeInUnits() * sizeof(DType); }
```

`Numel = Rows * Cols` 定义在 [pto_tile.hpp:1437](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1437)。跨后端通用的写法则是直接写 `Rows * Cols * sizeof(T)`（NPU 上没有这对函数）。

#### 4.2.4 代码实践

**实践目标**：建立"形状 → 字节数 → 容量判决"的手感。

**操作步骤**：

1. 对 tadd 的五种显式实例化（[tadd_kernel.cpp:54-L62](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L54-L62)：float 64×64、int32 64×64、int16 64×64、aclFloat16 16×256、bfloat16_t 16×256）逐一计算单 tile 字节数。
2. 填表：

| dtype | 形状 | 单 tile 字节 | 3 tile 合计 | ≤ 184KB 可用区？ |
|---|---|---|---|---|
| float | 64×64 | 0x4000 | 0xC000 | 是 |
| int32_t | 64×64 | 0x4000 | 0xC000 | 是 |
| int16_t | 64×64 | 0x2000 | 0x6000 | 是 |
| aclFloat16 | 16×256 | 0x2000 | 0x6000 | 是 |

3. 把 `int16_t` 用例的形状也改成 64×64 后重新合计，验证地址表 0x0/0x4000/0x8000 对它仍然安全（0x2000 < 0x4000，只是浪费一半）。

**需要观察的现象**：纸面推演即可，无需运行。

**预期结果**：tadd 的地址表按最大 dtype（4 字节 × 64×64）预留，每档 0x4000；所有实例化共用这张表都安全。合计 0xC000 远小于 184KB，也不碰 0x2E000 起的保留区。

#### 4.2.5 小练习与答案

**练习 1**：一个内核要处理 half 类型的 128×256 tile 共 6 块（含乒乓），A2A3 上 UB 够吗？

**答案**：单块 128×256×2 = 65536 = 0x10000，6 块共 0x60000 = 384KB > 192KB，**超了**。即便不算乒乓（3 块 = 192KB）也只是恰好顶满、还压掉保留区。必须缩小 tile（如 64×256，6 块共 192KB 仍不行；64×128 则 6 块共 96KB 可行）或减少缓冲份数。

**练习 2**：`ScaleLeft` tile 在 A2A3 上 TASSIGN 会发生什么？在哪一层被拦截？

**答案**：A2A3 的 `PTO_SCALELEFT_SIZE_BYTES = 0`。使用编译期地址重载 `TASSIGN<Addr>(tile)` 时会被 `tassign_static_check` 的 SA-0351 断言（"memory space is not available on this architecture"）在**编译期**拦截；NPU 编译时即便用运行期重载，绑出来的也是一块容量为 0 的空间，属于未定义行为。

**练习 3**：为什么 CPU 模拟器要把 UB 模拟成至少 512KB，而不是如实模拟 192KB？

**答案**：CPU 模拟器的定位是验证**数值语义**而非资源约束（u1-l4/u1-l5 的结论）。把 UB 放大是为了让"地址表偏大但逻辑正确"的内核在 CPU 上也能跑通功能，资源纪律交给 NPU 编译期的静态检查去守。反过来说，这也在提醒你：**CPU 跑通绝不能证明地址规划合法**。

### 4.3 乒乓地址规划：add_custom.cpp 的手工 UB 布局

#### 4.3.1 概念说明

流水线并行（u1-l4 讲过 MTE2→V→MTE3 三段）带来一个天然矛盾：MTE2 想提前搬入**下一块**数据，而 V 还在读**当前块**——同一块缓冲不能同时被写和读。解决方案就是**乒乓（ping-pong）双缓冲**：每个逻辑缓冲准备两份物理空间，`pingpong_flag` 在 0/1 间交替，搬入和计算永远操作不同的那份。地址规划因此从"给每个 tile 找一个床位"升级为"给每个 tile 找**两个**床位，且两套床位互不重叠"。

`demos/baseline/add/csrc/kernel/add_custom.cpp` 是仓库里手工 UB 布局最完整的样板。注意它是**板端 demo**：文件第 [11 行](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L11)的 `#if __CCE_AICORE__ == 220 && defined(__DAV_C220_VEC__)` 使它只在 A2/A3 的 C220 向量核上编译——CPU 模拟器跑不了它，但它的地址规划方法论可以直接照搬。

#### 4.3.2 核心流程

```text
add_custom 的 UB 地址地图（A2A3, 总 0x30000）
────────────────────────────────────────────
0x00000 ┌────────────────────────┐
        │ X_PING  0x0            │ 输入 x 的乒乓对
0x08100 │ X_PONG                 │   （段预算 0x8000 + 0x100 guard）
        │  ...余量...             │
0x10000 ├────────────────────────┤
        │ Y_PING  0x10000        │ 输入 y 的乒乓对
0x18100 │ Y_PONG                 │
        │  ...余量...             │
0x20000 ├────────────────────────┤
        │ Z_PING  0x20000        │ 输出 z 的乒乓对
0x28100 │ Z_PONG                 │
        │  ...余量...             │
0x30000 └────────────────────────┘  ← 192KB 上限
```

主循环每轮：等 MTE2/V/MTE3 的相应事件 → TLOAD x/y 到 `xTiles[pp]`/`yTiles[pp]` → TADD 到 `zTiles[pp]` → TSTORE → `pp = 1 - pp`。搬入第 i+1 块（pong）与计算第 i 块（ping）得以重叠。

#### 4.3.3 源码精读

地址表以常量写在文件头部，[add_custom.cpp:18-L30](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L18-L30)：

```cpp
constexpr uint32_t BLOCK_DIM = 20;                     // number of vector cores(AIVs)
constexpr uint32_t BUFFER_NUM = 2;                     // ping-pong buffer
constexpr unsigned UB_SIZE = 0x30000;                  // 192KB UB of A2A3
constexpr unsigned X_PING = 0x0;                       // ping address of input x in UB buffer
constexpr unsigned X_PONG = (X_PING + 0x8000 + 0x100); // pong address of input x in UB buffer
constexpr unsigned Y_PING = 0x10000;
constexpr unsigned Z_PING = 0x20000;
constexpr unsigned MAX_TILE_SIZE = (0x10000 - 0x100);  // Maximum tile size
constexpr uint32_t tileNum = 2;                        // tile number on one vector core
```

三个值得咀嚼的细节：

1. **段预算而非实际用量**：每个 64KB 段（X/Y/Z）内部，ping 放段首、pong 放 `段首 + 0x8100`。0x8000（32KB）是"单个 tile 缓冲的尺寸预算"，0x100 是 guard 余量。实际 tile 是 `tileSRows×tileSCols` = 1×512 half（`tileCols=2048` 除以 `tileNum×BUFFER_NUM=4` 得 512，见 [L43-L45](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L43-L45)），只占 1KB——预算远大于实际用量，地址表以"好记的段边界 + 充足余量"换取可读性和日后调 tile 尺寸的弹性。
2. **核间切分先于地址规划**：`tileRows=20, tileCols=2048` 是**单个向量核**分到的任务，`BLOCK_ROWS×BLOCK_COLS` 把它再切给核内 tile（[L37-L45](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L37-L45)）。UB 预算约束的是切分后的 tile。
3. **编译期守门**，[L41](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L41)：

```cpp
static_assert(bTileRows * bTileCols * sizeof(T) <= MAX_TILE_SIZE, "UB buffer overflow.");
```

tile 越大该断言越早失败，这就是 4.4 节"用户侧溢出断言"的仓库范例。

tile 的定义与绑定，[L54-L71](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L54-L71)：每个逻辑缓冲是"长度 BUFFER_NUM 的 tile 数组"（`TileData xTiles[BUFFER_NUM]`，动态 mask 构造传入 `vRows/vCols`），然后六连 TASSIGN 把六个物理槽位一次绑好——注意 TASSIGN 全部在循环**外**完成，循环内只切换下标：

```cpp
TileData xTiles[BUFFER_NUM] = {TileData(vRows, vCols), TileData(vRows, vCols)};
...
TASSIGN(xTiles[0], X_PING);
TASSIGN(xTiles[1], X_PONG);
TASSIGN(yTiles[0], Y_PING);
TASSIGN(yTiles[1], Y_PONG);
TASSIGN(zTiles[0], Z_PING);
TASSIGN(zTiles[1], Z_PONG);
```

主循环 [L73-L109](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L73-L109) 中 `pingpong_flag` 同时驱动两件事：tile 下标 `xTiles[pingpong_flag]` 和事件 ID `(event_t)(pingpong_flag)`（EVENT_ID0/ID1 交替，事件的完整语义留到 u3-l1/u3-l3），循环尾 `pingpong_flag = (pingpong_flag == 0) ? 1 : 0;` 完成交替。而循环内对 GlobalTensor 的 `TASSIGN(xGlobal, x + iterOffset)`（[L86-L88](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L86-L88)）则是 u2-l2 讲过的"移动 GM 窗口"——同一名字两种 TASSIGN 在同一个循环里协作，正好对照复习。

#### 4.3.4 代码实践

**实践目标**：通过改参数理解地址表与切分参数的联动。

**操作步骤**：

1. 纸面修改 `BUFFER_NUM` 为 4（四缓冲）。为每个逻辑缓冲排 4 个地址：`X0=0x0, X1=0x8100, X2=0x10200, X3=0x18300`。
2. 检查：X 段第 4 块结束于 0x18300 + tileBytes；`Y_PING` 仍在 0x10000 吗？
3. 再把段预算从 0x8100 改为"实际用量 + 对齐"（1KB tile 向上取整到 32 的倍数），重排整张表。

**需要观察的现象**：步骤 2 会发现 0x18300 + tileBytes 已经越过 0x20000（Z 段），即四缓冲在"每段 64KB、预算 0x8100"的旧格局下放不下——必须整体重排（例如压缩 guard、或 X/Y/Z 改用实际用量背靠背排布）。

**预期结果**：得出结论——** BUFFER_NUM 翻倍近似让每段占用翻倍**，地址表不是孤立的常量集合，而是与 `tileNum × BUFFER_NUM × tileCols ≤ 段预算` 这条不等式联动的设计。板端行为**待本地验证**（本文件 CPU 模拟器不可编译）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 TASSIGN 要放在循环外，而 GM 窗口 TASSIGN 放在循环内？

**答案**：UB 槽位只有 2×3 个且地址固定，绑定一次即可；每轮循环处理的是 GM 的不同切片，GlobalTensor 的窗口必须每轮平移（`x + iterOffset`）。这正是"片上静态布局、全局动态游标"的分工。

**练习 2**：地址表里到处出现的 `+0x100` guard 起什么作用？去掉它行不行？

**答案**：在"预算尺寸"与"下一段起始"之间留 256 字节空隙，防止预算估算误差或后续 tile 变大后越过分界、静默踩到下一段数据。去掉它在本例中大概率仍能跑（实际 tile 远小于预算），但失去了对规划误差的缓冲——仓库示例宁可浪费一点 UB 也不要边界紧贴，这是值得效仿的工程习惯。

**练习 3**：add_custom 用 `UB_SIZE = 0x30000` 做整体上限，这与 4.2 节说的 184KB 可用区矛盾吗？

**答案**：不矛盾但偏宽松。地址表实际最高用到 0x28100 + tile 预算，远未触及 0x2E000；`UB_SIZE` 只是 static_assert 的宽松上界。若某张表真排到 0x2E000 之后，就可能被使用 `TMP_UB_OFFSET` 临时区的指令（如 a2a3 的 TExtract/TCvt）踩到数据——严谨的做法是以 0x2E000 为界。

### 4.4 编译期溢出断言：tassign_static_check 与用户 static_assert

#### 4.4.1 概念说明

手工地址表的两类错误——**越界**（超出缓冲容量）与**失配对齐**（地址不是 32 的倍数）——都最好在编译期暴露。PTO 提供两层防线：

1. **框架层**：编译期地址重载 `TASSIGN<Addr>(tile)` 触发 `tassign_static_check`，按 TileType 自动查出容量与对齐，做四条断言（编号 SA-0351~SA-0354）。
2. **用户层**：像 add_custom 那样，在内核里用 `static_assert` 显式校验自己的地址表总预算。

两层互补：框架层只看"单个 tile + 单个地址"，用户层能把"多块 tile × 乒乓份数"的总账算进去。

#### 4.4.2 核心流程

```text
TASSIGN<0x4000>(tile)  （Addr 是模板参数）
    │
    ├─ detail::tassign_static_check<TileT, Addr>
    │     ├─ BufferTraits<TileT::Loc> → capacity / alignment / 缓冲名
    │     ├─ tile_bytes = Rows × Cols × sizeof(DType)   （ConvTile 用 bufferSize）
    │     └─ 四条 static_assert：
    │          SA-0351  capacity > 0              （该架构存在这块存储吗）
    │          SA-0352  tile_bytes ≤ capacity     （tile 本身装得下吗）
    │          SA-0353  Addr + tile_bytes ≤ capacity  （越界吗）
    │          SA-0354  Addr % alignment == 0     （对齐吗）
    │
    └─ 委托运行期 TASSIGN(obj, Addr)
```

关键限制：`__CPU_SIM` 与 `__COSTMODEL` 下这套检查是**空结构体**（CPU 模拟器不建模片上容量，4.2 节已解释原因），只有 NPU 目标才启用。

#### 4.4.3 源码精读

先看开关，[include/pto/common/tassign_check.hpp:17-L29](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/tassign_check.hpp#L17-L29)：CPU 模拟与 CostModel 分支只定义空的 `tassign_static_check`，注释写明"CPUSIM does not model on-chip buffer capacities, so skip all static checks"；NPU 分支才 include buffer_limits 并给出完整实现。

`TileStorageBytes` 计算占地，[tassign_check.hpp:41-L58](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/tassign_check.hpp#L41-L58)：普通 Tile 是 `Rows * Cols * sizeof(DType)`，ConvTile 用 `bufferSize * sizeof(DType)`——与 4.2 节的手算公式完全一致。

`BufferTraits` 把 TileType 映射到 buffer_limits 的宏，[tassign_check.hpp:64-L128](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/tassign_check.hpp#L64-L128)，例如：

```cpp
template <>
struct BufferTraits<TileType::Vec> {
    static constexpr std::size_t capacity = PTO_UBUF_SIZE_BYTES;
    static constexpr std::size_t alignment = PTO_UBUF_ALIGN_BYTES;
    static constexpr const char* name = "UB";
};
```

九个 TileType 各有一份特化，`name` 字符串用于报错时指名是哪块缓冲。

四条断言本体，[tassign_check.hpp:140-L179](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/tassign_check.hpp#L140-L179)。每条都带 SA 编号、人话描述与修复建议（FIX-A12），例如 SA-0353：

```cpp
static_assert(
    capacity == 0 || end_addr <= capacity,
    "[SA-0353] TASSIGN: addr + tile_size exceeds memory space capacity "
    "(out of bounds). Use a smaller address or reduce tile size. (Fix: FIX-A12)");
```

`end_addr = Addr + tile_bytes`。这套 SA 编号与 docs/coding/error-codes.md 的错误码体系对应（u7-l5 会系统讲调试与错误码）。

用户层的范例回到 [add_custom.cpp:41](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L41)——它没有用模板地址重载（地址常量是运行期传入的普通常量表达式），而是自己算账并断言。这种写法**在所有后端（包括 CPU 模拟器）都会生效**，因为它就是普通的 C++ 编译期算术，不依赖框架的检查开关。综合实践会照这个样式写。

#### 4.4.4 代码实践

**实践目标**：亲手触发并读懂一个编译期断言。

**操作步骤**：

1. 在 CPU 模拟器的任一测试内核副本里，给一个 64×64 float tile 写 `TASSIGN<0x30000 - 0x1000>(tile)`（编译期重载）。
2. 编译：`python3 tests/run_cpu.py -t tadd`（或你复制出的用例名）。
3. 再写一条用户级断言：`static_assert(0x30000 - 0x1000 + 64 * 64 * 4 <= 0x30000, "over");` 观察这条是否报错。

**需要观察的现象**：步骤 2 中 CPU 模拟器**不会**因 `TASSIGN<...>` 编译失败——SA 检查在 `__CPU_SIM` 下是空操作；而步骤 3 的 `static_assert` 是纯编译期算术，条件为假（0x2F000 + 0x4000 = 0x33000 > 0x30000）必然报错。

**预期结果**：体会到两层防线的分工——框架检查只护 NPU 编译，用户 static_assert 全后端生效；给 CPU 模拟器写内核时，容量纪律要自己用断言守。**待本地验证**（依赖本地编译环境）。

#### 4.4.5 小练习与答案

**练习 1**：`TASSIGN<0x1>(tile)`（地址 1）在 NPU 上会触发哪条断言？为什么不是 SA-0353？

**答案**：SA-0354（对齐失败）。`1 % 32 != 0`。SA-0353 查的是越界，地址 1 加上 tile 大小通常仍在容量内，先撞上的是对齐检查。四条断言按声明顺序求值，SA-0351/0352 关于"空间是否存在、tile 是否装得下"，SA-0353/0354 才是"这个地址行不行"。

**练习 2**：一个 64×512 的 int32 tile 在 A2A3 上，`TASSIGN<Addr>` 的 Addr 合法区间是什么？

**答案**：tile_bytes = 64×512×4 = 0x40000 = 256KB > 192KB（PTO_UBUF_SIZE_BYTES），先触发 SA-0352——这个 tile 在 A2A3 UB 里**根本放不下**，不存在合法区间。要么改 half（0x20000，且需 Addr ≤ 0x10000 才不越界，同时避开 0x2E000 保留区则更紧），要么换 A5（UB 256KB，恰好放下但 Addr 只能为 0，且无保留余量——实际上仍不推荐）。

**练习 3**：既然框架已有 SA 检查，为什么 add_custom 还要自己写 static_assert？

**答案**：三个原因——(a) SA 检查只覆盖"单 tile 单地址"，管不了"多 tile × 乒乓份数"的总账与段预算；(b) SA 检查在 CPU 模拟/CostModel 下关闭，用户断言全后端生效；(c) 它的约束对象是**切分后的 tile 尺寸**（随 BLOCK_DIM/tileNum 变化），用 static_assert 把不等式固化下来，改参数时第一时间爆错。

## 5. 综合实践

把本讲四个模块串成一个任务：**为"4 个 8×256 float tile + 乒乓"的内核手写一张 UB 地址分配表，用 static_assert 校验总量不超过 0x30000，并在 CPU 模拟器上跑通。**

### 5.1 任务设计

内核 `taddsub`：输入 GM 上的 x、y（各 8×512 float），分两轮（tileNum=2）处理；每轮 TLOAD 一个 8×256 切片到乒乓槽，计算 z = x + y（TADD）与 w = x − y（TSUB，公共签名见 [include/pto/common/pto_instr.hpp:207-L210](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_instr.hpp#L207-L210)），TSTORE 两组结果。涉及 4 类 tile：x、y、z、w，各 BUFFER_NUM=2 份，共 8 个物理槽。

### 5.2 地址分配表（示例代码）

先算账：单 tile = 8 × 256 × 4 = 0x2000 字节；8 槽共 0x10000，远小于 0x30000，也远离 0x2E000 保留区；所有地址都是 0x2000 的倍数，天然满足 32 字节对齐。

```cpp
// —— 以下为示例代码：仿照 add_custom.cpp 的地址规划风格 ——
constexpr unsigned UB_SIZE = 0x30000;        // A2A3 192KB（见 buffer_limits.hpp）
constexpr unsigned TILE_BYTES = 8 * 256 * sizeof(float);  // 0x2000
constexpr uint32_t BUFFER_NUM = 2;           // ping-pong
// 背靠背排布：x → y → z → w，各占 ping/pong 两槽
constexpr unsigned X_PING = 0x0;
constexpr unsigned X_PONG = X_PING + TILE_BYTES;
constexpr unsigned Y_PING = X_PONG + TILE_BYTES;
constexpr unsigned Y_PONG = Y_PING + TILE_BYTES;
constexpr unsigned Z_PING = Y_PONG + TILE_BYTES;
constexpr unsigned Z_PONG = Z_PING + TILE_BYTES;
constexpr unsigned W_PING = Z_PONG + TILE_BYTES;
constexpr unsigned W_PONG = W_PING + TILE_BYTES;
// 编译期总量校验：所有后端（含 CPU 模拟器）都生效
static_assert(W_PONG + TILE_BYTES <= UB_SIZE, "UB buffer overflow.");
static_assert((X_PING % 32) == 0 && (W_PONG % 32) == 0, "UB address must be 32B aligned.");
```

### 5.3 内核骨架（示例代码）

```cpp
// —— 以下为示例代码，基于 tadd_kernel.cpp 改写 ——
template <typename T, int kTRows_, int kTCols_>
AICORE void runTAddSub(__gm__ T __out__* outZ, __gm__ T __out__* outW,
                       __gm__ T __in__* src0, __gm__ T __in__* src1)
{
    using ShapeDim5  = Shape<1, 1, 1, kTRows_, kTCols_>;      // 单切片窗口
    using StrideDim5 = Stride<1, 1, 1, kTCols_ * 2, 1>;       // 大矩阵列数 = 2 个切片
    using GlobalData = GlobalTensor<T, ShapeDim5, StrideDim5>;
    using TileData = Tile<TileType::Vec, T, kTRows_, kTCols_, BLayout::RowMajor, -1, -1>;

    TileData xTiles[BUFFER_NUM] = {TileData(kTRows_, kTCols_), TileData(kTRows_, kTCols_)};
    TileData yTiles[BUFFER_NUM] = {TileData(kTRows_, kTCols_), TileData(kTRows_, kTCols_)};
    TileData zTiles[BUFFER_NUM] = {TileData(kTRows_, kTCols_), TileData(kTRows_, kTCols_)};
    TileData wTiles[BUFFER_NUM] = {TileData(kTRows_, kTCols_), TileData(kTRows_, kTCols_)};
    TASSIGN(xTiles[0], X_PING);  TASSIGN(xTiles[1], X_PONG);
    TASSIGN(yTiles[0], Y_PING);  TASSIGN(yTiles[1], Y_PONG);
    TASSIGN(zTiles[0], Z_PING);  TASSIGN(zTiles[1], Z_PONG);
    TASSIGN(wTiles[0], W_PING);  TASSIGN(wTiles[1], W_PONG);

    GlobalData xGlobal(src0), yGlobal(src1), zGlobal(outZ), wGlobal(outW);
    int8_t pp = 0;
    for (uint32_t i = 0; i < 2; i++) {              // tileNum = 2 轮
        TASSIGN(xGlobal, src0 + i * kTRows_ * kTCols_);   // 移动 GM 窗口（u2-l2）
        TASSIGN(yGlobal, src1 + i * kTRows_ * kTCols_);
        TASSIGN(zGlobal, outZ + i * kTRows_ * kTCols_);
        TASSIGN(wGlobal, outW + i * kTRows_ * kTCols_);

        TLOAD(xTiles[pp], xGlobal);
        TLOAD(yTiles[pp], yGlobal);
        set_flag(PIPE_MTE2, PIPE_V, (event_t)pp);
        wait_flag(PIPE_MTE2, PIPE_V, (event_t)pp);
        TADD(zTiles[pp], xTiles[pp], yTiles[pp]);   // z = x + y
        TSUB(wTiles[pp], xTiles[pp], yTiles[pp]);   // w = x - y
        set_flag(PIPE_V, PIPE_MTE3, (event_t)pp);
        wait_flag(PIPE_V, PIPE_MTE3, (event_t)pp);
        TSTORE(zGlobal, zTiles[pp]);
        TSTORE(wGlobal, wTiles[pp]);
        set_flag(PIPE_MTE3, PIPE_V, (event_t)pp);
        wait_flag(PIPE_MTE3, PIPE_V, (event_t)pp);
        pp = 1 - pp;
    }
}
```

### 5.4 落地步骤（CPU 模拟器）

1. **复制四件套**：`cp -r tests/cpu/st/testcase/tadd tests/cpu/st/testcase/taddsub`，按 u1-l4 的"三处同步"约定改名：`CMakeLists.txt` 改为 `pto_cpu_sim_st(taddsub)`；`tadd_kernel.cpp` 换成上面的内核并加显式实例化 `LaunchTAddSub<float, 8, 256>`；`main.cpp` 新增 `TEST_F(TADDSUBTest, case_float_8x256)`。
2. **生成数据**：改 `gen_data.py`，生成 8×512 的 x、y 输入和 z=x+y、w=x−y 两组 golden（numpy 一行 `z = x + y; w = x - y`，注意按列优先/行优先与 Stride 一致）。
3. **运行**：`python3 tests/run_cpu.py -t taddsub`（CPU 模拟器入口，u1-l2 讲过；不要用 `run_st.py`，那是 sim/npu 模式且需要 CANN 环境）。
4. **观察**：gtest 输出 PASS；`--verbose` 可看构建日志。可选 `--trace-mode` 打开指令 trace，核对 TASSIGN/TLOAD/TADD/TSUB/TSTORE 的实际执行序列。

### 5.5 预期结果与检查点

- `static_assert` 全部通过编译（总量 0x10000 ≤ 0x30000）。
- golden 比对通过：z 与 w 的每个元素误差为 0（float 精确加减）。
- 思考题自检：把 `TILE_BYTES` 改成"实际用量 0x2000 + 0x100 guard"会怎样？（总量变 8×0x2100=0x10800，仍安全；若把形状放大到 32×256，单 tile 0x8000、8 槽共 0x40000 > 0x30000，static_assert 立即拦截——这就是你要的现象。）

运行结果**待本地验证**（本讲义写作环境未执行）。

## 6. 本讲小结

- **TASSIGN 是绑定不是分配**：把整数字节偏移写进 Tile 的 `data_`；NPU 上变成 `__ubuf__ T*` 等地址空间限定指针（Auto 模式下是空操作），CPU 模拟器上映射进 `NPUMemoryModel` 的模拟区域。重叠、越界它都不查。
- **地址相对各自缓冲从 0 编号**：TileType 决定落到 UB/L1/L0A/L0B/L0C 哪块存储；GlobalTensor 的同名 API 收指针、移 GM 窗口，一词两用。
- **容量查 buffer_limits.hpp**：A2A3 UB 192KB（0x30000）、A5/A6 256KB、Kirin 系列 128KB；全部 32 字节对齐；UB 尾部 0x2E000 起 8KB 是 PTO 自留临时区，别压。
- **乒乓地址规划 = 每 tile 两个不重叠槽位**：add_custom 的样板是"段预算 + guard 余量"，TASSIGN 在循环外一次绑好、循环内只切下标；地址表与 `tileNum × BUFFER_NUM × tile 尺寸` 联动。
- **两层编译期防线**：框架的 `TASSIGN<Addr>(tile)` 触发 SA-0351~0354（仅 NPU 生效）；用户 static_assert 自算总账（全后端生效，add_custom L41 是范例）。CPU 模拟器故意不模拟容量紧缺，跑通不等于规划合法。

## 7. 下一步学习建议

本讲补完了"数据抽象"的最后一块（地址绑定），下一讲进入**单元三：同步与流水线**：

- **u3-l1 事件与同步模型**：本讲反复出现的 `set_flag/wait_flag`、`(event_t)pp` 到底如何为 MTE2→V→MTE3 提供顺序保证——这是读懂 add_custom 主循环的钥匙。
- **u3-l3 乒乓缓冲与多核切分实战**：把 add_custom 的 `BLOCK_ROWS×BLOCK_COLS` 核间切分与乒乓事件 ID 交替讲透。
- 预习建议：重读 [add_custom.cpp:79-L113](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L79-L113) 的事件序列，标出每条 set/wait 保护的缓冲是 ping 还是 pong，带着问题进下一讲。

# u7-l4 架构取舍与局限：硬编码、禁用分支与改进方向

## 1. 本讲目标

这是学习手册的收官之讲。前面六单元我们沿着调用链逐层下潜，本讲退后一步，站在系统设计者的视角复盘整个 BitDecoding 仓库：

1. **系统盘点硬编码点**：哪些配置被写死在编译期（hdim=128、fp16、kBlockN=256、kBlockM=16、sm_80/sm_90），它们如何互相联动，动一处会牵动哪里。
2. **画出禁用分支地图**：dispatch 层 12 个分支只有 4 个真正活着，k-tensor 模式、group_size=64、paged-KV、非 split 前向都被注释或掏空——逐一列出证据并分析原因。
3. **算清 3-9x 加速的代价账本**：LOP3 反量化进 Tensor Core、FP16 残余区、split-KV 占用率三者的收益与代价如何互相牵制。
4. **形成可落地的二次开发路线图**：扩展到 bf16、hdim=64、新架构各需要改哪些文件；Python 缓存层有哪些隐藏成本。

学完本讲，你应该能独立完成一份架构评审报告——这也是本讲的综合作业。

## 2. 前置知识

本讲不再引入新的源码细节，而是把已学知识重新组织。先统一几个评审用的术语：

- **编译期 vs 运行期**：`num_bits`、`quant_mode`、`group_size`、head_dim、tile 尺寸都是 C++ 模板参数，在编译时就固化成不同的 kernel 二进制；运行期 `if` 链（dispatch）只是把 Python 传来的参数路由到对应的模板实例。**编译期能取的值 = genfile 里显式实例化的那几个**，多一个都要重新编译（见 u7-l3）。
- **SASS 与二进制兼容**：nvcc 给 `sm_80` 生成的 SASS（真机指令）可以在同大版本的 sm_86/sm_89 上运行，所以 4090（sm_89）能跑只编了 `sm_80` 的位码；但**每线程块共享内存上限**是硬约束：sm_86/89 为 100 KiB，sm_80（A100）为 163 KiB，sm_90（H100）为 227 KiB——这直接决定了 2-bit kernel 只能在 sm_80/sm_90 上启动（待本地验证具体数值）。
- **有效比特数**：每个 KV 元素实际占 \( b + 32/g \) 比特（\(b\) 为量化位宽，\(g\) 为 group_size，params 是 fp32 的 scale/zero）。这是评估一切带宽收益的出发点（u7-l2 已建立）。
- **死代码（dead code）**：仓库里保留但永远不会被执行的函数或分支，通常是从上游 FlashAttention 复制改造时的遗留。识别死代码是架构评审的基本功——**读代码时不能假设"存在的就是工作的"**。
- **消融（ablation）**：固定其他变量、只开关一个机制（如残余区、split）来度量它贡献的实验方法。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [README.md](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/README.md) | 项目门面 | 3-9x 声明与评测证据（4090/A100 图） |
| [csrc/bit_decode/decode_api.cpp](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp) | pybind 绑定与 dispatch | 12 分支 dispatch 的启停状态、写死的 block_n=256 |
| [csrc/bit_decode/src/include/kernel_traits.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h) | 编译期骨架 | 常量派生链、SharedStorage 容量、架构条件编译 |
| [csrc/bit_decode/src/flash_fwd_launch_template.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h) | kernel 启动层 | 写死的 kBlockM=16/kBlockN=256、sm80-sm90 架构守卫、空函数 |
| [bit_decode/models/cache_utils.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py) | 改造版缓存 | torch.cat 拼接的隐藏拷贝成本 |
| [setup.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/setup.py) | 构建脚本 | 只编 sm_80/sm_90、只编 5 个 genfile 源 |
| genfile 实例化文件（`csrc/bit_decode/src/genfile/*.cu`） | 模板实例化 | 哪些配置真的被编译出来 |

## 4. 核心概念与源码讲解

### 4.1 编译期写死的骨架：kernel_traits 常量与固定 tile

#### 4.1.1 概念说明

BitDecoding 的所有限制中，最底层的是**编译期硬编码**：配置一旦编译就不可变。这不是偷懒，而是性能使然——tile 形状、共享内存布局、MMA 指令选择、拷贝原子的线程排布全部依赖编译期常量，CuTe 模板才能把它们编排成零开销的访问模式。代价是：换一个 head_dim 或位宽，就要重新推导一整套常量并重新实例化。

要评审的是**这些写死的值之间不是独立的，而是一张联动网**：`kBlockN=256` → `kBlockN_pack` → `residual_block_size` → paged-KV 的 256 整除检查，牵一发而动全身。

#### 4.1.2 核心流程

kernel_traits 的常量派生链（u5-l1 已精读，这里从架构视角复盘）：

```text
num_bits ──► pack_num = 16/num_bits ──► kBlockN_pack (4bit→128, 2bit→256)
                │                            │
                │                            └──► residual_block_size = kBlockN_pack
                └──► kHeadDim_pack = kHeadDim / pack_num

group_size ──► kHeadDim_v_params = kHeadDim / group_size
           ──► num_params = kBlockN_pack / group_size   （要求 ≤ 8，见 u4-l2）

kBlockN (启动层写死 256) ──► kBlockP、kBlockK_params、tile_paramsk_* 全家族
```

启动层则把 M/N 两个 tile 维度也钉死：

```cpp
constexpr static int kBlockM = 16;   // Fixed for all head dimensions
constexpr static int kBlockN = 256;
```

#### 4.1.3 源码精读

**写死的 tile 尺寸**（启动层）：

- [csrc/bit_decode/src/flash_fwd_launch_template.h:130-137](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L130-L137)：`run_mha_fwd_splitkv_dispatch` 中 `kBlockM = 16`、`kBlockN = 256` 直接写死，且第 133 行保留了 FlashAttention 原版按 head_dim 自适应的注释——说明作者**有意**放弃了自适应，换取打包 tile 与残余块的整齐对齐。

**常量派生链**（traits 层）：

- [csrc/bit_decode/src/include/kernel_traits.h:83-93](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L83-L93)：`kBlockN_pack`（第 83 行）与 `kBlockN_residual`（第 84 行）相等，即**残余块大小被硬绑定到打包 tile**；`kHeadDim_pack`、`kHeadDim_v_params` 等由 `kHeadDim=128` 派生。第 75 行 `residual_block_size = num_bits == 4 ? 128 : 256` 再次强调这个绑定。
- [csrc/bit_decode/src/include/kernel_traits.h:98](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L98)：`static_assert(kHeadDim % 32 == 0)` 是 hdim 扩展的第一道闸门；第 315 行还有 `kHeadDim % kGmemElemsPerLoad == 0`（128 位向量装载要求 hdim 是 8 的倍数）。

**两套 MMA 的取舍**：

- [csrc/bit_decode/src/include/kernel_traits.h:117-127](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L117-L127)：`TiledMma`（16×128×16）服务 FP16 路径；`TiledMmaKV_i4`（16×32×16）专供反量化后的打包数据。N 维从 128 缩到 32，正是 LOP3 管线的代价之一：**每次 MMA 覆盖的 K/V 列更窄，指令条数增多**，换取的是加载字节降为 1/4~1/8。

**共享内存容量**：

- [csrc/bit_decode/src/include/kernel_traits.h:284-308](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L284-L308)：`SharedStorage` 六块视图与 `SharedStorage_residual` 五块视图，`kSmemSize`/`kSmemSize_res` 是启动时 `cudaFuncSetAttribute` 的依据。按 u5-l1 的推导，splitkv kernel 约 77/78 KiB（4-bit）到 144/148 KiB（2-bit），**全部超过 48 KiB 的静态上限**，这就是为什么启动代码必须抬限，也是 2-bit 无法在 100 KiB 上限的 sm_86/89 上运行的根本原因。
- [csrc/bit_decode/src/include/kernel_traits.h:22-28](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L22-L28)：`__CUDA_ARCH__ >= 800` 条件编译，sm_75 及以下退化成无 cp.async 的半残路径（实际上整个仓库根本不为 sm_75 编译，见 4.5）。

**架构守卫**：

- [csrc/bit_decode/src/flash_fwd_launch_template.h:14-22](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L14-L22)：`ARCH_SUPPORTS_FLASH` 宏与错误信息 "requires building with sm version sm80-sm90" 把支持的架构范围写进了一条 printf。

#### 4.1.4 代码实践：编译期可行性审计（纸面即可完成）

1. **实践目标**：对假设的新配置 `(num_bits=2, group_size=64, head_dim=64, quant_mode=k-channel)` 做一次"编译期可行性审计"，找出所有会失败或变形的常量。
2. **操作步骤**：
   - 按上面派生链手算：`pack_num=8`，`kBlockN_pack=256`，`kHeadDim=64`，`kHeadDim_pack=8`，`kHeadDim_v_params=1`，`num_params=256/64=4`；
   - 逐条核对 [kernel_traits.h:98](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L98) 与第 315 行的 `static_assert`；
   - 再检查 [kernel_traits.h:213-218](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L213-L218)：`SmemLayoutAtomV` 以 `kHeadDim_pack` 为行宽，hdim=64 且 2-bit 时行宽只有 8 个 uint16=16 字节，Swizzle<3,3,3>（128 字节粒度）是否还有意义？
3. **需要观察的现象**：hdim=64 时哪些断言直接编译失败、哪些能编译但布局退化（Swizzle 失效、bank conflict 回潮）。
4. **预期结果**：`kHeadDim % 32 == 0` 通过（64%32=0），静态断言不拦；真正的问题在派生布局的语义退化——这类"编译能过但性能/正确性可疑"的配置是最危险的。若要实测，需按 u7-l3 的三层配对法新增实例化（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `kBlockN_pack` 对 4-bit 取 128、对 2-bit 取 256，而不是统一取 256？

**答案**：两种位宽下 `kBlockN_pack / pack_num` 都等于 32（128/4 = 256/8 = 32），即打包后 tile 恒占 32 行 uint16。这使 K/V 打包区的共享内存布局、拷贝原子的线程排布（[kernel_traits.h:338-349](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L338-L349) 的 `(32,4)`/`(64,2)` 布局）在两种位宽下结构一致，同时让打包 tile 的 uint16 总数（`kBlockP × kHeadDim_pack`）保持稳定，控制 smem 规模。

**练习 2**：`residual_block_size` 能不能不经过改 kernel 直接从 128 改成 64？

**答案**：不能（对当前实现而言）。第 75 行和第 84 行把 `residual_block_size ≡ kBlockN_pack ≡ kBlockN_residual` 焊死，残余区在 kernel 里就是一整块 smem tile；Python 侧传入的 `residual_block_size` 参数在 [decode_api.cpp:333](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L333) 只是签名里的哑参数，从未被消费（u3-l1 已证）。要改它必须改 kernel_traits 并重新实例化。

**练习 3**：`set_params_splitkv` 里 `num_m_blocks` 按 64 一块计算，而 kernel 实际 `kBlockM=16`，这个不一致有影响吗？

**答案**：[decode_api.cpp:292-294](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L292-L294) 的注释自己承认"kBlockM = 64 only for the splitKV kernels"是过时说法。decode 时 `seqlen_q` 为 1（GQA 重排后为 ngroups，一般 ≤64），两种算法都得到 `num_m_blocks=1`，所以**当前无实际影响**；但若 GQA 组数超过 64（如 128 个 Q 头映射到 1 个 KV 头），启发式会低估并行度。这是一个典型的"潜伏不一致"，评审时应记录。

### 4.2 dispatch 层的禁用分支地图：12 个分支只有 4 个活着

#### 4.2.1 概念说明

dispatch 是编译期配置与运行期参数的交接点。u3-l1 讲过它的机制（if 链落空会静默无操作），本讲从**架构视角**回答：哪些分支被禁用了？禁用状态在几处代码之间如何成对出现？以及——被禁用的分支是"没写完"还是"写完又关掉"？

答案藏在一个三方配对结构里（u7-l3 建立）：**dispatch 分支 ↔ genfile 显式实例化 ↔ setup.py 源列表**。genfile 中被注释的实例化与 dispatch 中被注释的分支几乎一一对应，说明这些路径是**写完、测过、然后主动关闭**的——代码存在，实例化被裁剪，最可能是编译时间与二进制体积的权衡（每个实例化都是一份完整 kernel）。

#### 4.2.2 核心流程

decode_api.cpp 中两个 dispatch 函数共 12 个分支（`2 个函数 × 2 种 quant_mode × 3 种 group_size`）：

```text
run_mha_fwd (解码注意力)          run_kvcache_qpack (打包)
├─ k-channel                      ├─ k-channel
│   ├─ g=128  ✅ 启用             │   ├─ g=32   ✅ 启用
│   ├─ g=64   ❌ 注释             │   ├─ g=64   ❌ 注释
│   └─ g=32   ✅ 启用             │   └─ g=128  ✅ 启用
└─ k-tensor                       └─ k-tensor
    ├─ g=128  ❌ 注释                 ├─ g=32   ❌ 注释
    ├─ g=64   ❌ 注释                 ├─ g=64   ❌ 注释
    └─ g=32   ❌ 注释                 └─ g=128  ❌ 注释

（每条启用分支经 num_bits 模板覆盖 2-bit 与 4-bit 两个绑定函数）
```

此外还有三类"更彻底的禁用"：整段注释掉的通用 dispatch 宏、被掏空的函数体、以及预留但从未实例化的 paged-KV 路径。

#### 4.2.3 源码精读

**主 dispatch 的启停**：

- [csrc/bit_decode/decode_api.cpp:196-217](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L196-L217)：`run_mha_fwd` 的 if 链。k-channel 的 g=128（第 201 行）与 g=32（第 205 行）启用；g=64（第 203 行）与整个 k-tensor 分支（第 208-214 行）全部注释。注意第 185-195 行被注释的 `FP16_SWITCH/HEADDIM_SWITCH` 宏——那是 FlashAttention 原版的通用 dispatch，作者换成手写 if 链以精确控制实例化集合。
- [csrc/bit_decode/decode_api.cpp:219-238](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L219-L238)：`run_kvcache_qpack` 同构：k-channel g=32/g=128 启用，其余注释。

**genfile 侧的成对裁剪**（证据）：

- [csrc/bit_decode/src/genfile/flash_fwd_split_hdim128_fp16_sm80_4bit.cu:7-14](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_fwd_split_hdim128_fp16_sm80_4bit.cu#L7-L14)：k-tensor 三条（第 7-9 行）与 k-channel g=64（第 13 行）被注释，只留第 12、14 行两条实例化——与 dispatch 的启停**逐行镜像**。
- [csrc/bit_decode/src/genfile/flash_qpack_hdim128_fp16_sm80_4bit.cu:7-32](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/genfile/flash_qpack_hdim128_fp16_sm80_4bit.cu#L7-L32)：qpack 侧同样：k-channel 两条特化（第 7-18 行）启用，k-tensor 三条（第 21-32 行）全注释。

**被掏空的函数**：

- [csrc/bit_decode/src/flash_fwd_launch_template.h:140-173](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L140-L173)：`run_mha_fwd_hdim128` 函数体完全为空，里面是原版按 sm8x 分支选 tile 的注释代码——非 split 的标准前向 kernel 路径被整体废弃（decode 永远 force split，见 [decode_api.cpp:519](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L519) 的 `force_split_kernel=true`）。第 71-74 行的 `run_flash_fwd` 同样是空壳。

**paged-KV：参数预留、路径未通**：

- [csrc/bit_decode/src/include/flash.h:157-160](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L157-L160)：`block_table`、`page_block_size` 等字段在参数结构体里**已经预留**；
- [csrc/bit_decode/decode_api.cpp:351-358](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L351-L358) 与 [csrc/bit_decode/decode_api.cpp:375](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L375)：host 侧完整做了 block_table 校验，甚至要求 `page_block_size % 256 == 0`（256 正是 kBlockN，分页块要对齐 tile）；
- 但 [csrc/bit_decode/src/flash_fwd_launch_template.h:90-98](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L90-L98)：kernel 实例化时模板实参写死 `..., /*Split*/true, false, false`——最后那个 `false` 就是 `Paged_KV`。**管道铺好了，阀门没打开**；而且 Python 侧 DynamicCache 是连续存储（见 4.4），根本没有分页的生产者。

#### 4.2.4 代码实践：绘制禁用分支对照表

1. **实践目标**：产出一张覆盖 12 个 dispatch 分支 × 三层配对点的完整对照表，作为评审报告的附件。
2. **操作步骤**：
   - 用 Grep 在 `csrc/bit_decode/decode_api.cpp` 中搜索 `group_size ==`，统计启用/注释行号；
   - 在 `csrc/bit_decode/src/genfile/` 的 4 个 2/4-bit 文件中搜索 `run_mha_fwd_splitkv_dispatch<` 与 `run_kvcache_qpack_<`，记录每个显式实例化/特化及其注释状态；
   - 核对 [setup.py:129-136](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/setup.py#L129-L136) 的 5 个 `.cu` 源文件是否覆盖所有启用实例化所在的文件。
3. **需要观察的现象**：dispatch 与 genfile 的启停是否严格成对；2-bit 与 4-bit 的 genfile 是否对称。
4. **预期结果**：得到一张 12 行的表，其中 4 行三层全通（k-channel × {128,32} × {splitkv, qpack}），其余 8 行至少 dispatch 与实例化两层同时被注释。这正是 u7-l3 实践（启用 g=64）的出发点——本表就是那次实践的前置调研。

#### 4.2.5 小练习与答案

**练习 1**：如果只解开 dispatch 里 k-tensor 的注释、不解开 genfile 的实例化，会发生什么？

**答案**：链接错误。dispatch 引用 `run_mha_fwd_splitkv_dispatch<..., 0, num_bits, g>` 的符号，而 genfile 没有实例化它，链接器找不到定义。反过来只解实例化不解 dispatch，则是死代码（编译进二进制但无人调用）。两层都关才是现在看到的"静默落空"——if 链不命中，函数直接返回未初始化的输出（u3-l1）。

**练习 2**：作者为什么要保留被注释的 k-tensor 代码，而不是删掉？

**答案**：从工程角度这是"特性开关"式管理：k-tensor 的 kernel 代码（如 `quant_Ktensor`、`SmemLayoutKPacktransposed_` 的转置分支、quad 归约族）都完整存在，注释只是裁剪实例化。保留它们让 reopen 的成本降低到"取消两层注释 + 重新编译"，也向读者宣示设计上支持该模式。代价是仓库的可读性负担——初学者很难分辨"存在"与"可用"。

**练习 3**：paged-KV 校验里为什么要求 `page_block_size % 256 == 0`？

**答案**：256 是写死的 `kBlockN`（[decode_api.cpp:290](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L290)）。分页注意力里 block_table 把逻辑块号映射到物理块，kernel 每次处理一个 kBlockN tile；若页大小不是 tile 的整数倍，一个 tile 会跨页，索引逻辑复杂化。要求页大小 ≡ 0 (mod tile) 保证每块 tile 落在整数个物理页内。这是"tile 常量向上渗透到 API 校验"的又一例证。

### 4.3 3-9x 的代价账本：LOP3、残余区与 split-KV 的三方权衡

#### 4.3.1 概念说明

README 声称相对 Flash Attention v2 加速 3-9x。本模块把这个数字拆开，回答三个问题：收益从哪来、代价付在哪、三个核心机制如何互相牵制。

三个机制各有一本账：

- **LOP3 反量化进 Tensor Core**（收益：带宽；代价：指令与 tile）：反量化在寄存器内联完成、不落共享内存（u5-l3），KV 读取字节降为 fp16 的 \( (b+32/g)/16 \)。但 MMA 的 N 维从 128 缩到 32（4.1），指令条数增多——这是用计算换带宽，在 memory-bound 的 decode 里划算，但意味着**上下文越短、batch 越大，收益越薄**。
- **FP16 残余区**（收益：精度；代价：固定的 fp16 tile 与 smem）：最新 `residual_block_size` 个 token 永远以 FP16 参与 MMA，规避近端 token 的量化误差放大（u2-l2）。代价是 residual kernel 恒占一个累积槽位、一块 fp16 tile 的 smem，以及 4.4 将算的 Python 侧拼接成本。
- **split-KV**（收益：SM 占用率；代价：fp32 中间缓冲与 combine kernel）：decode 时并行度只有 batch×heads，split 沿 KV 维切分把 SM 喂饱，但每个 split 写 `out_accum`/`softmax_lse_accum` 的流量与 split 数成正比，还要多启动一个 combine kernel（u3-l3、u5-l5）。

#### 4.3.2 核心流程

加速上界由有效比特数决定。每元素比特 \( b_{\text{eff}} = b + 32/g \)，理想情况下 kernel 加速上界：

\[ S_{\max} = \frac{16}{b + 32/g} \]

代入四个启用配置：

| 配置 | \( b_{\text{eff}} \) | \( S_{\max} \) | 备注 |
|---|---|---|---|
| 4-bit, g=128 | 4.25 | ≈ 3.76x | 带宽节省最多但量化误差最大 |
| 4-bit, g=32 | 5.0 | 3.2x | params 开销占 64% 的位宽 |
| 2-bit, g=128 | 2.25 | ≈ 7.1x | 误差显著（u1-l4 实测） |
| 2-bit, g=32 | 3.0 | ≈ 5.3x | — |

README 的 3-9x 区间正是这个上界扣除反量化指令开销、残余区 fp16 读取、combine 流量后的落点（kernel 级口径，见 u7-l2：端到端还会被权重读取稀释）。而残余区带来的额外读取可以量化：每步对每层每个 (head, dim) 行，打包区读 \( (b+32/g)/8 \) 字节/token，残余区读 fp16 的 4 字节/token（K+V 各 2 字节），残余区 token 的读取成本约为打包区的 \( 32/(b+32/g) \) 倍——但残余只占 \( R/L \) 的比例，L 很长时带宽影响可忽略，**真正的代价在 smem 与精度延迟**（下面练习展开）。

#### 4.3.3 源码精读

**README 的证据链**：

- [README.md:5-7](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/README.md#L5-L7)：项目定位与 3-9x 声明（HPCA 2026 论文 "BitDecoding: Unlocking Tensor Cores for Long-Context LLMs with Low-Bit KV Cache"）。
- [README.md:11-15](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/README.md#L11-L15)：kernel 级性能图分别给 RTX 4090 与 A100——注意 4090 是 sm_89，只能跑 4-bit（2-bit 的 smem 超 sm_89 的 100 KiB 上限），这本身就是一张"架构 × 配置"兼容性矩阵的注脚。

**split-KV 的代价代码**：

- [csrc/bit_decode/decode_api.cpp:240-280](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L240-L280)：`num_splits_heuristic` 的注释直说权衡——"不想太多 split 因为那会增加 HBM 读写"，于是取达到峰值效率 85% 的最小 split 数（第 274 行）。85% 是经验常数，评审时可质疑其普适性。
- [csrc/bit_decode/decode_api.cpp:282-313](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L282-L313)：`set_params_splitkv` 第 306-307 行按 `num_splits × b × h × seqlen_q × d` 分配两个 fp32 累积缓冲——split 数每 +1，这两个缓冲的分配与读写都 +1 份；第 303 行 `num_splits += 1` 给 residual kernel 留槽位（u3-l3）。
- [csrc/bit_decode/src/flash_fwd_launch_template.h:106-127](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L106-L127)：combine kernel 按 `Log_max_splits` 阶梯选择特化——split 上限 128 就是这里 7 档阶梯的来源。

**残余区的代价代码**：

- [csrc/bit_decode/src/flash_fwd_launch_template.h:85-96](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L85-L96)：residual kernel 独立以 `grid=(num_m_block, b, h)` 启动，与 splitkv kernel（第 86 行 `grid=(num_m_block, num_splits_, b*h)`）分开——每步 decode 是**两个注意力 kernel + 一个 combine kernel** 的固定开销，短上下文时这个三连发占主导。

**权重与 KV 的稀释**（回顾）：u7-l2 的结论在此适用——3-9x 是 kernel 级数字，模型级 decode 每步还要读权重，收益被稀释；论文图的正确打开方式是与 `bench_throughput.py` 同口径对比。

#### 4.3.4 代码实践：设计 residual_block_size 权衡实验

1. **实践目标**：设计一个控制变量的实验，量化 `residual_block_size` \( R \) 对精度与性能的影响，并先做理论预测。
2. **操作步骤**：
   - **理论侧**（纸面）：写出 R 的硬约束集——\( R \equiv kBlockN_{pack} \)（kernel 绑定，4.1）；\( R \bmod pack\_num = 0 \) 且 \( R \bmod g = 0 \)（块内整组量化）；\( R \ge g \)（保证每块至少一个完整量化组）。推导残余 tile 的 smem：\( R \times d \times 2\,\text{B} \)（K、V 各一块，见 [kernel_traits.h:168-170](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L168-L170) 与第 227-229 行的 `SmemLayoutKResidual/VResidual`），算出 R=256 时 2-bit 的 smem 总量逼近 sm_80 上限的程度。
   - **实验侧**（需 GPU，待本地验证）：固定模型、prompt 与量化配置，扫 \( R \in \{128, 256\} \)（当前 kernel 只支持这两档，见练习 3），指标取 kernel 级延迟（复用 [csrc/bit_decode/src/bench_single_residual.cu](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/bench_single_residual.cu) 的 cudaEvent 口径）、Python 级逐轮 MAE（`evaluation/test.py`）、端到端 GSM8K 准确率（`example.py`）。
3. **需要观察的现象**：R 增大后 MAE 是否下降（新 token 在 FP16 区停留更久）；kernel 延迟是否基本不变（残余 tile 恒为一块）；`v_pack.shape[1]` 的跳变间隔是否随之变为 256（u6-l2 的锯齿规律）。
4. **预期结果**：精度提升、kernel 延迟近似持平——因为残余区每步的 fp16 读取量只与 R 线性相关而 R 远小于 L。若实测延迟上升明显，说明 smem 接近上限导致 occupancy 下降，这本身就是有价值的评审发现。

#### 4.3.5 小练习与答案

**练习 1**：为什么说"上下文越长、位宽越低，BitDecoding 收益越大"？用 \( S_{\max} \) 公式和 kernel 固定开销两方面回答。

**答案**：带宽上界 \( S_{\max}=16/(b+32/g) \)，2-bit 时可达 5-7x，且上下文越长 KV 读取在 decode 阶段占比越大、上界越接近被兑现；同时每步固定的三 kernel 启动与 residual/splitkv 的固定 tile 工作量被更长的 KV 迭代摊薄。反之短上下文 + 大 batch 时，权重读取与固定开销占主导，低比特化的边际收益趋近于零。

**练习 2**：group_size 从 32 提到 128，\( S_{\max} \) 从 3.2x 升到 3.76x（4-bit），代价是什么？

**答案**：量化组变大 4 倍，组内 max/min 覆盖的数值范围变宽，scale 变粗，量化误差上界 \( \text{scale}/2 \) 变大（u4-l3）。也就是说 group_size 是"带宽 ↔ 精度"的直接旋钮：\( 32/g \) 比特的 params 开销换 \( g \) 个元素共享一组 scale/zero。这也是为什么仓库同时实例化 g=32 与 g=128 两档——留给用户按精度需求选择。

**练习 3**：85% 规则（[decode_api.cpp:274](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L274)）和 `num_splits += 1`（第 303 行）分别防的是什么极端情况？

**答案**：85% 规则防"split 过多"——效率曲线有多个波峰，盲目取最高峰会用很多 split 换一点点效率，而每个 split 都要付出 fp32 累积缓冲的读写；取峰值 85% 的最小 split 数是在占用率与 HBM 流量之间折中。`+= 1` 防"残余没槽位"——residual kernel 的部分结果也要写进 `out_accum` 的一个槽位参与 combine，若不预留，splitkv kernel 会把最后一个槽位也占掉。两个设计都体现同一原则：**并行度不是越大越好，要为固定开销和合并成本付账**。

### 4.4 Python 缓存层的隐藏成本：torch.cat 与 O(L²/R) 拷贝

#### 4.4.1 概念说明

kernel 层的设计非常精细，但 Python 缓存层走了一条完全不同的工程路线：**整文件复制 transformers 的 cache_utils.py 再局部修改**（u6-l1）。这带来三个架构层面的局限：

1. **动态增长靠拼接**：主缓存每攒满一个残余块就 `torch.cat` 一次，旧数据整体重拷——随上下文变长，累计拷贝量是 \( O(L^2/R) \) 级别。
2. **无容量规划**：`get_max_cache_shape` 返回 None，永远不能对接 StaticCache / paged / torch.compile 生态。
3. **复制式维护**：整文件复制官方代码意味着每次 transformers 升级都可能产生漂移（u6-l1 已发现 `get_seq_length` 语义漂移）。

这不是作者的疏忽，而是"研究原型优先跑通"的典型取舍——认清它，才知道工程化时要补什么。

#### 4.4.2 核心流程

设最终缓存长度为 \( L \)（token 数），残余块大小 \( R \)，单个 token 的四类缓存（k_pack/k_params/v_pack/v_params）合计每层每头每维字节数为 \( c \)。每次攒满触发一次 `torch.cat`，第 \( i \) 次拼接拷贝约 \( iR \) 个 token 的数据：

\[ C_{\text{cat}} \approx \sum_{i=1}^{L/R} i \cdot R \cdot c = \frac{R \cdot \frac{L}{R}(\frac{L}{R}+1)}{2} \cdot c \approx \frac{L^2}{2R} \cdot c \]

以 L=100k、R=128、4-bit/g=128 为例：\( \frac{L^2}{2R} \approx 3.9\times10^7 \) 个 token 份的拷贝——是最终缓存本身的 \( L/(2R) \approx 390 \) 倍。此外每次 cat 还会产生新旧两份共存的瞬时显存峰值，且拼接发生在 GPU 上、由 Python 逐步触发，长生成任务中这些碎片化的大拷贝会与 kernel 计算抢带宽。

#### 4.4.3 源码精读

- [bit_decode/models/cache_utils.py:465-474](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L465-L474)：改造版 `DynamicCache.__init__` 维护 6 个列表——原 `key_cache`/`value_cache` 被复用为 FP16 残余区，新增 `*_pack`/`*_params` 四组低比特主缓存（u6-l1）。
- [bit_decode/models/cache_utils.py:657-660](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L657-L660)：`update_pack` 的四路 `torch.cat(...).contiguous()`——K 系沿 dim=-3、独 v_params 沿 dim=-1（布局差异在 u2-l1/u3-l2 已解释）。这四行就是 \( O(L^2/R) \) 的来源：**每次都把整个旧缓存读出再写入新分配的更大张量**。
- [bit_decode/models/cache_utils.py:679-681](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L679-L681)：`get_max_cache_shape` 返回 `None`——容器层面宣布自己无上界、无法预分配。这是与 vLLM 类 serving 框架（预分配 + 分页）最根本的不兼容点，也解释了 4.2 中 paged-KV 为什么没有上游生产者。
- [bit_decode/models/cache_utils.py:664-666](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L664-L666)：`clear_residual` 直接置空列表——残余区"清空"是引用替换而非原地擦除，配合 `update_residual` 的追加，残余张量每步都是新建的（`torch.cat` 产物），又一份小而频繁的分配。

#### 4.4.4 代码实践：量化拼接成本

1. **实践目标**：用一段独立脚本（只需 CPU + PyTorch）实测 `torch.cat` 增长策略的拷贝量曲线，验证 \( O(L^2/R) \) 推导。
2. **操作步骤**：
   - 写一个 15 行的脚本：模拟 `L=4096`、`R=128`，每次向一个形状类似 k_pack 的张量 `(1, s/4, 8, 128)`（uint16）cat 一个 `s=R` 的新块，循环到 L；用 `tensor.untyped_storage().nbytes()` 或计数器记录每轮拷贝的元素数；
   - 对照组：预分配到最大长度、用切片写入（模拟 StaticCache 思路），同样计数；
   - 画出两条累计拷贝曲线（可用 matplotlib，或直接打印每 512 token 的累计值）。
3. **需要观察的现象**：动态 cat 的累计拷贝量呈二次增长，预分配版本为线性（只写新块）。
4. **预期结果**：L=4096、R=128 时动态策略累计拷贝约为最终缓存的 \( L/(2R)=16 \) 倍；把 L 或 1/R 翻倍，倍数线性放大。此实验纯 CPU 可复现（待本地验证具体数值）。进一步思考：如果把 cat 换成分块预分配（每次预留 2 倍空间、均摊拷贝），复杂度可降到均摊 \( O(L \cdot 2c) \)——这正是 Python 侧 list/vector 的经典增长策略。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `update_pack` 里 K 系沿 dim=-3 而 v_params 沿 dim=-1？这个差异如何反过来加重 cat 的成本？

**答案**：K 系（k_pack/k_params/v_pack）的序列维在 dim=-3，v_params 的序列维在 dim=-1（u2-l1 的布局结论：V 参数把序列放末维保证同组参数内存连续）。cat 本身按维度语义拼接、成本相同，但 `v_params` 沿最后一维拼接意味着新旧数据在**最内层**交错扩展，拼接后必须保证连续性（`.contiguous()` 的防御性调用），其访问局部性在后续 kernel 读取时才体现收益——布局为 kernel 优化，代价由 Python 拼接承担。

**练习 2**：如果把主缓存改成"预分配 + 写指针"（类似 StaticCache），需要动哪些层？

**答案**：至少三层联动：(1) Python 层——`DynamicCache` 改为持有预分配张量与长度计数器，`update_pack` 变成切片写入；(2) 绑定层——`mha_fwd_kvcache` 的 `seqlen_k` 目前从 `v_pack.size(1)` 推断（[decode_api.cpp:372](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L372)），预分配后必须显式传入有效长度（`cu_seqlens_k` 已被复用为该语义，u3-l2）；(3) kernel 层——掩码与拷贝谓词已按 `seqlen_k_cache` 处理（u5-l2 的 BlockInfo），主要工作是打通而非重写。这个改造是通往 serving 友好的必经之路。

**练习 3**：`get_max_cache_shape() -> None` 会阻断哪些上游功能？

**答案**：transformers 的 `get_usable_length` 依赖它判断容量（[cache_utils.py:171-180](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L171-L180)）；返回 None 意味着无法参与 StaticCache 式的容量协商、无法配合 `torch.compile` 的静态图要求（`is_compileable=False`），也使 beam search 的 `reorder_cache`（只重排 FP16 残余区，低比特缓存四组列表根本没被重排——见 [cache_utils.py:182-190](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L182-L190) 只遍历 `key_cache/value_cache`）产生静默的错误结果。评审时应把"beam search 与低比特缓存不兼容"列为已知局限。

### 4.5 扩展路线图：bf16、hdim64、新架构与工程化

#### 4.5.1 概念说明

把前面三个模块的发现转化成行动清单。扩展分三个方向，难度递增：

- **换数据类型（fp16→bf16）**：管道里有现成的条件分支，但 LOP3 魔数是 FP16 专用的。
- **换形状（hdim128→hdim64/256）**：常量派生链大部分自适应，但布局原子与拷贝排布需重推导，且要新增 genfile 实例化。
- **换架构（sm_90 原生 / 未来）**：sm_90 已编译但只是沿用 SM80 MMA（没用 TMA/WGMMA），sm_100+ 需要重写数据通路。

#### 4.5.2 核心流程

三类扩展的通用流程（承接 u7-l3 的三层配对法）：

```text
① 编译期可行性检查（4.1 的 static_assert 与常量派生链）
        ↓
② kernel_traits 适配（布局原子、拷贝排布、SharedStorage 重算）
        ↓
③ genfile 新增显式实例化/特化
        ↓
④ decode_api dispatch 解开或新增分支
        ↓
⑤ setup.py 源列表 / cc_flag 确认
        ↓
⑥ evaluation/test.py 逐轮 MAE 验证 + bench 对比
```

#### 4.5.3 源码精读

**bf16：管道半通**：

- [csrc/bit_decode/decode_api.cpp:59](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L59)：`set_params_fprop` 已计算 `params.is_bf16`，但 dispatch（第 197-205 行）全部硬编码 `cutlass::half_t`，`is_bf16` 从未被消费；
- [csrc/bit_decode/src/include/kernel_traits.h:33-41](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L33-L41)：MMA 原子已有 `SM80_16x8x16_F32BF16BF16F32_TN` 的条件分支，MMA 层面 bf16 是现成的；
- **真正的拦路虎是 LOP3**：u5-l3 精读的 0x6400 指数魔数利用的是 FP16 的指数字段布局（指数字段 25 使尾数最低位权重为 1），bf16 只有 8 位指数、7 位尾数，布局完全不同，SUB/MUL/ADD 三常量（1024、1/16、−64）都要重新推导。此外 params 的 smem 视图是 `__half2`（[kernel_traits.h:290](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L290)），量化参数侧也要跟着换。

**hdim64/256：闸门与暗礁**：

- [csrc/bit_decode/decode_api.cpp:377](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L377) 与 [csrc/bit_decode/decode_api.cpp:649-650](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L649-L650)：host 侧只拦 `head_size <= 256` 与 `% 8 == 0`，看起来很宽松；但 kernel 侧只实例化了 128。第 185-195 行注释掉的 `HEADDIM_SWITCH` 宏显示原版 FA2 按 head_dim switch 的机制——恢复它（或手写分支）是 hdim 扩展的第一步；
- 暗礁在拷贝原子：[kernel_traits.h:338-354](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L338-L354) 的 `(32,4)`/`(64,2)` 线程排布是与 `kHeadDim_pack=32/16`（hdim128 时）配套推导的，hdim 变了这些数字要重算（4.1 实践已演练过 hdim=64 的退化）。

**架构支持：编译目标**：

- [setup.py:111-117](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/setup.py#L111-L117)：恒编 `sm_80`，CUDA ≥ 11.8 追加 `sm_90`——注意**没有** sm_86/sm_89 的 gencode，4090 靠 sm_80 SASS 的同大版本兼容性运行（因此受 100 KiB smem 上限约束）；
- [setup.py:129-136](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/setup.py#L129-L136)：源列表固定 5 个 genfile 文件，新增配置若新建文件必须同步此处（u7-l3）；
- sm_90 上跑的是 SM80 路径的代码（SM80 MMA + cp.async），没有利用 TMA/WGMMA——H100 上的 3-9x 图实际是"老管线吃新带宽"，这既说明设计可移植，也说明 sm_90 优化空间未挖掘。

**工程化改进的证据锚点**（供评审报告引用）：

- 猴子补丁与复制式维护：u6-l1（`example.py` 替换 transformers 缓存类、整文件复制 modeling 文件）；
- dispatch 静默落空：u3-l1（无 TORCH_CHECK 兜底）；
- 测试金字塔断层：u7-l1（两个 .cu 测试是旧签名的 API 化石，只有 test_single_residual 与现 API 同步）。

#### 4.5.4 代码实践：撰写 bf16 / hdim64 改动清单

1. **实践目标**：为"bf16 支持"与"hdim=64 支持"各产出一份逐文件的改动清单（本讲综合实践的第 1 项）。
2. **操作步骤**：
   - 对每个候选改动点，先用 Grep 确认现状：搜 `half_t`（decode_api.cpp 与 genfile 中的模板实参）、`0x6400`（dequantize.h）、`__half2`（kernel_traits.h 的 smem 视图）、`kHeadDim`（traits 常量与断言）；
   - 按难度给每项标注【配置/中等/研究级】：如"dispatch 换 elem_type"是配置级，"LOP3 bf16 常量族推导"是研究级；
   - 为每项写出验证手段（哪条测试或 bench 能证明它工作）。
3. **需要观察的现象**：bf16 清单里研究级条目集中在反量化；hdim64 清单里研究级条目集中在拷贝原子与 Swizzle 布局。
4. **预期结果**：两张清单，各自约 6-10 条。参考骨架——bf16：① dispatch 模板实参（decode_api.cpp:197-227）→ ② genfile 用 bf16 实例化 → ③ flash.h/params 的 elem 透传 → ④ dequantize.h 魔数族重推导 → ⑤ qpack 侧量化计算改 bf16 输入 → ⑥ test.py 增加 bf16 参考对比。hdim64：① HEADDIM_SWITCH 恢复或手写分支 → ② traits 常量链自查 → ③ 拷贝原子排布重推导 → ④ 新 genfile 文件 + setup.py 源列表 → ⑤ `seqlen_k_rounded` 等舍入逻辑核对（decode_api.cpp:401-405）→ ⑥ smem 容量重算与 cudaFuncSetAttribute 核对。全部待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么说"sm_90 支持是真的，sm_90 优化是没做的"？

**答案**：编译上 sm_90 被包含（setup.py:114-117），架构守卫也放行（sm8x || sm90，decode_api.cpp:343-347），kernel 能跑。但所有数据通路都是 SM80 时代的：`SM80_CP_ASYNC_CACHEGLOBAL` 拷贝（kernel_traits.h:321-325）、`SM80_16x8x16` MMA、无 TMA、无 WGMMA、无 warp specialization。sm_90 的 227 KiB smem 上限倒是让 2-bit 的 144 KiB 布局毫无压力——这是当前设计在 H100 上"意外受益"的地方。

**练习 2**：项目自己声称支持的 head_size 上限是 256（decode_api.cpp:377），实际呢？

**答案**：实际只有 128 被实例化（genfile 文件名全部是 `hdim128`）。host 校验的 256 是从 FlashAttention 复制来的宽松上限，给了用户"能跑 256"的错误预期——若真传 hdim=256 的张量，dispatch 无分支可命中，静默返回垃圾（u3-l1 的陷阱）。评审时应把这归类为"校验与能力不匹配"缺陷。

**练习 3**：如果要给这个项目排三项最优先的改进，你会怎么选？给出理由框架（不要求与参考答案一致）。

**答案**（参考）：一个合理的框架是"正确性风险 > 可用性 > 峰值性能"：(1) **dispatch 落空加 TORCH_CHECK**——静默返回未初始化内存是最容易坑死用户的缺陷，改动只有几行；(2) **缓存层预分配/分块增长**——消除 \( O(L^2/R) \) 拷贝并打开 serving 兼容之门（配合打开 paged-KV 管道）；(3) **补齐正确性测试**（把两个 API 化石测试升级到现签名，纳入 CI）——任何后续优化都需要安全网。相比之下 bf16/新配置属于"能力扩展"，优先级应低于这三项。你的排序可以不同，但必须能自圆其说。

## 5. 综合实践

**任务：撰写一份《BitDecoding 架构评审报告》**，这是全手册的毕业设计。报告必须至少覆盖以下四节，所有论断都要附源码永久链接或前面讲义的实验证据：

1. **hdim=128 / fp16 / sm_80 的支持面影响与扩展改动清单**：
   - 用 4.5 的两张清单（bf16、hdim64）为基础，补充"影响面分析"——哪些用户/模型（如 head_dim=64 的 Gemma、bf16 训练的模型）现在无法使用；
   - 明确指出"声称支持 vs 实际支持"的差距（head_size ≤ 256 校验 vs 只有 hdim128 实例化）。
2. **k-tensor 模式被注释的原因假设与验证实验**：
   - 至少给出两个假设（例如：A. 二进制体积/编译时间裁剪——12 个分支全开编译时间翻倍；B. k-tensor 精度或性能不达预期——transposed 布局（`SmemLayoutKPacktransposed_`）带来 bank conflict 或 LDSM 效率下降；C. 论文消融后选择了 k-channel，保留代码作复现线索）；
   - 为每个假设设计可证伪的实验：按 u7-l3 三层配对法解注释一个 k-tensor 配置，跑 `evaluation/test.py` 比 MAE（验证 A/B 的"能不能跑"与"精度"）、跑 `bench_single_*.cu` 口径比延迟（验证 B 的性能），并检查编译时间变化（验证 A）。标注哪些步骤需 GPU、待本地验证。
3. **residual_block_size 与精度/带宽的权衡实验设计**：
   - 复用 4.3.4 的设计，补充统计口径（重复次数、warmup、报告 min/avg/max）、混淆变量控制（同 seed、同 prompt 集合）、预期结论与判伪条件（如"若 R=256 时 MAE 不降反升，则说明量化延迟并非误差主导因素"）。
4. **最值得优先做的三项改进及理由**：
   - 用 4.5 练习 3 的框架陈述你的排序，每项给出：问题证据（源码链接）、改动范围估计（文件/行级）、验证手段、风险。

写完后自查三件事：每个"局限"是否都有代码证据？每个"改进"是否都考虑了三层配对（dispatch/实例化/构建）？每个"实验"是否写明了统计口径？

## 6. 本讲小结

- **硬编码是一张联动网**：`kBlockN=256` → `kBlockN_pack` → `residual_block_size` → paged 校验的 256 整除，tile 常量从 kernel 一路渗透到 API 层；改任何一处都要按派生链全局自查。
- **dispatch 12 个分支只有 4 个活着**：k-tensor 与 g=64 在 dispatch 与 genfile 两层成对注释，paged-KV 参数预留但模板实参写死 `Paged_KV=false`，`run_mha_fwd_hdim128` 是空壳——"存在的代码"不等于"可用的路径"。
- **3-9x 的账本**：上界 \( S_{\max}=16/(b+32/g) \)（3.2x~7.1x），扣除 LOP3 小 tile 的指令开销、残余区 fp16 读取、split 的 fp32 中间缓冲与 combine kernel 后落进 3-9x 区间；上下文越长、位宽越低，收益越足。
- **三方权衡**：LOP3 用计算换带宽、残余区用 smem/槽位换精度、split-KV 用 HBM 流量换占用率——三个机制各付各的账，`num_splits_heuristic` 的 85% 规则与 `num_splits+1` 是两处精妙的平衡点。
- **Python 层是短板**：`torch.cat` 增长带来 \( O(L^2/R) \) 累计拷贝，`get_max_cache_shape()=None` 阻断 serving/compile 生态，`reorder_cache` 不重排低比特缓存（beam search 静默出错）。
- **扩展路线**：bf16 卡在 LOP3 魔数族，hdim64 卡在拷贝原子排布，sm_90 能跑但未用新特性；一切扩展遵循"可行性检查 → traits 适配 → genfile 实例化 → dispatch → setup.py → 测试验证"六步。

## 7. 下一步学习建议

- **对照上游**：把这个仓库与 [flash-attention](https://github.com/Dao-AILab/flash-attention) v2.3 前后的 `flash_fwd_splitkv` 实现做 diff 阅读，会清楚看到"哪些是继承、哪些是改造、哪些是裁剪"——这是理解任何 fork 型项目最快的方式。
- **补论文**：精读 BitDecoding 论文（HPCA 2026，arXiv:2503.18773）的消融章节，验证本讲 4.3 的权衡账本与论文口径是否一致，特别关注 k-tensor/k-channel 的取舍叙述与仓库禁用分支的对应关系。
- **横向对比**：阅读 KIVI（逐通道 KV 量化的先驱）与 Atom/Flute（低比特权重反量化进 Tensor Core）的实现，比较三者"量化布局 ↔ kernel 设计"的耦合方式，思考 BitDecoding 的 uint16 打包 + LOP3 方案在什么场景会被替代。
- **动手方向**：从综合实践的评审报告里挑一项真正实施——推荐先做"dispatch 落空加 TORCH_CHECK"（几行代码、消除最大用户陷阱），再做缓存层分块预分配；每一步都用 `evaluation/test.py` 的逐轮 MAE 做回归安全网。至此全手册 24 讲完毕，你已具备从位级技巧到系统取舍的完整视角。

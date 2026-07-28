# 数据搬运：T.copy 与 T.view

## 1. 本讲目标

学完本讲后，你应该能够：

- 看懂 `T.copy(src, dst, ...)` 的**切片语法**：传入 `Buffer`、`BufferLoad`（带切片）或 `BufferRegion` 时，搬运范围（extent）是如何被推导、对齐与广播的。
- 理解同一个 `T.copy` 在编译期会被**自动选路**为不同的底层指令——TMA（Bulk Load/Store，含 1D 与多维）、`ldmatrix`/`stmatrix`（LDSM/STSM）、`tcgen05`（tensor memory）或普通 SIMT 拷贝——以及 `disable_tma`、`coalesced_width`、`eviction_policy` 这几个旋钮的作用。
- 掌握 `T.view` / `T.reshape` 的本质：**零拷贝视图**，它们复用同一块底层存储（同一个 `.data` 指针），只换一个「形状/数据类型」的解释，并且必须满足「比特总数守恒」这一硬约束。
- 认识 `T.c2d_im2col`：卷积里把图像重排成矩阵列（im2col）的专用搬运，在 Hopper 上直接落到 TMA-im2col 指令。
- 把上述知识串成一个可运行的小 kernel（综合实践）。

本讲承接 [u2-l2](./u2-l2-tile-alloc.md) 的「显存层级与 tile 分配」，是后续 u3（编译流水线，尤其是 `LowerTileOp` 与 `LayoutInference`）和 u4（TMA / 软件流水）的前置。

## 2. 前置知识

在动手之前，先用一句话回顾几个会反复出现的概念（细节见 [u2-l2](./u2-l2-tile-alloc.md)）：

- **显存层级**：TileLang 把抽象 tile 绑定到不同 scope——`global`（HBM/显存）、`shared`/`shared.dyn`（block 内共享内存）、`local.fragment`（散布在 warp 线程上、对接 tensor core 的寄存器片段）、`local`（线程私有标量寄存器）。数据搬运的本质，就是让数据在这些层级之间流动。
- **tile 与 Buffer**：一个 tile 在 TIR 里就是一个 `tir.Buffer`；对它做切片 `A[i, j:k]` 得到的是 `tir.BufferLoad`/`tir.BufferRegion`，描述「这个 buffer 里的一个矩形子区域」。
- **「视图」是什么**：你有一块连续的内存，可以同时用多种方式去「读」它——同样 16 个 `float16` 元素，既能看成 `(4,4)` 的矩阵，也能看成 `(16,)` 的向量，还能看成 8 个 `float32`。只要总比特数不变，换一个解释是免费的，不需要搬动数据。这就是 `T.view` / `T.reshape`。
- **TMA / cp.async 的直觉**：从 global 搬一整块数据到 shared，老办法是开一串线程、每个线程搬几个元素（cp.async / ldg）；新硬件（Hopper 起）提供了 TMA，由「一个线程」发起、整块搬运、自带 swizzle 与边界处理，吞吐高、占用低。TileLang 的 `T.copy` 会在**编译期**替你决定用哪一种。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/copy_op.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py) | **`T.copy` 与 `T.c2d_im2col` 的活跃实现**：推导 extent、构造 region、最终拼出 `tl.tileop.copy` 这条 intrin。 |
| [tilelang/language/copy.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy.py) | `T.copy` 的**旧版**实现（生成 `tl.copy`）。当前 `__init__.py` **并未导入**它，C++ 也没有注册 `tl.copy`，仅作历史对照。 |
| [tilelang/language/customize.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/customize.py) | `T.reshape` / `T.view` 的实现：检查「比特总数守恒」后，复用 `src.data` 构造新 `T.Tensor`。 |
| [tilelang/language/proxy.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py) | `TensorProxy`：`T.Tensor(...)` 怎样由 `shape` 推出连续 row-major strides，以及如何复用传入的 `data` 指针。 |
| [tilelang/utils/language.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/language.py) | 工具函数：`to_buffer_region`（把 Buffer/切片编码成 `tl.region`）、`legalize_pairwise_extents`（左右对齐 + 广播）、`bits_product`（比特总数）、`get_buffer_region_from_load`（从 `BufferLoad` 反推 region）。 |
| [src/op/copy.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc) | C++ 侧 `tl.tileop.copy` / `tl.tileop.c2d_im2col` 的 **lowering**：按优先级选 TMA/LDSM/STSM/tcgen05/Normal，并真正生成 PTX 级别的拷贝语句。 |
| [src/op/copy.h](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.h) | `CopyNode` 的字段与注解读取（`GetDisableTMA`/`GetEvictionPolicy`）、`CopyInst` 枚举。 |
| [src/op/operator.h](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/operator.h) | 宏 `TIR_REGISTER_TL_TILE_OP`，把 C++ 算子注册成 `tl.tileop.<name>`，并挂上 builder。 |

## 4. 核心概念与源码讲解

### 4.1 T.copy 的切片语法与搬运语义

#### 4.1.1 概念说明

`T.copy(src, dst)` 的职责非常纯粹：**把 `src` 这块数据搬到 `dst` 这块**。它不关心你用的是 global 还是 shared、是整块还是切片、是矩阵还是向量——这些信息都藏在 `src` / `dst` 这两个参数里。

你会遇到三种写法：

```python
# 1) 整块搬运：src/dst 都是完整 Buffer，要求形状相同
T.copy(A_shared, B_shared)

# 2) 切片搬运：用 Python 切片语法指定子区域，这是最常见的 GEMM 写法
T.copy(A[bx * BM, 0:K], A_shared)
T.copy(kernel_flat[k_iter * BK, bx * BN], kernel_shared)

# 3) 标量搬运：两边都是单个元素，退化为一次赋值
T.copy(A[i], B[i])   # 等价于 B[i] = A[i]
```

关键在于：**搬运范围（extent）是从参数里「推导」出来的**，而不是你显式声明的。Buffer 用自己的 `shape`；切片用切片的 `[start:stop]` 长度；当两边形状不完全一致时，TileLang 还会做「尾部对齐 + 广播」。下面就看这套推导在源码里是怎么实现的。

#### 4.1.2 核心流程

`T.copy` 在 Python 前端做的事情，可以归纳成五步：

1. **整块校验**：若两边都是完整 `Buffer`，断言两者 `shape` 结构相同。
2. **推导 extent**：分别对 `src`、`dst` 调 `get_extent`——`Buffer` 给 `shape`、`BufferRegion` 给每维 `[r.extent]`、`BufferLoad` 先尝试反推出 region 再给 extent。
3. **标量快速通道**：若两边都推不出 extent（都是标量 `BufferLoad`），直接降级成一条 `tir.BufferStore`（`B[i] = A[i]`），不走拷贝原语。
4. **广播对齐**：把缺失的一侧补成全 1，再用 `legalize_pairwise_extents` 从尾部对齐：相等则保留、某侧为 1 则广播成另一侧、动态冲突则保守取 `tir.max`。
5. **编码成 region 并发出 intrin**：用 `to_buffer_region` 把 src/dst 编码成 `tl.region` 表示，把 `coalesced_width/disable_tma/eviction_policy` 收进 `annotations` 字典，最后发出 `tl.tileop.copy`。

伪代码如下：

```
copy(src, dst, *, coalesced_width, disable_tma, eviction_policy, annotations):
    se = get_extent(src); de = get_extent(dst)
    if se is None and de is None and 都是标量 BufferLoad:
        return BufferStore(dst, src)            # 退化为赋值
    se = se or [1]*len(de);  de = de or [1]*len(se)
    se, de = legalize_pairwise_extents(se, de)  # 尾部对齐+广播
    src = to_buffer_region(src, "r", extents=se)
    dst = to_buffer_region(dst, "w", extents=de)
    ann = 合并(coalesced_width, disable_tma, eviction_policy)
    return call_intrin("tl.tileop.copy", src, dst, annotations=ann)
```

#### 4.1.3 源码精读

`T.copy` 的活跃实现是 `copy_op.copy`（注意是 `copy_op.py`，不是 `copy.py`）。它的签名已经把所有旋钮亮出来了：

[tilelang/language/copy_op.py:L14-L21](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L14-L21) —— 定义 `copy(src, dst, coalesced_width, disable_tma, eviction_policy, annotations)`。

extent 推导的核心是内嵌的 `get_extent`，它按「Buffer → shape、BufferRegion → 每维 extent、BufferLoad → 先反推 region」三种情况返回：

[tilelang/language/copy_op.py:L59-L72](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L59-L72) —— `get_extent`：从 `Buffer`/`BufferRegion`/`BufferLoad` 三种入参推导搬运范围。

当两边都推不出 extent（典型场景 `copy(buffer_a[i], buffer_b[i])`）时，走标量快速通道，直接退化成一条 store：

[tilelang/language/copy_op.py:L77-L81](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L77-L81) —— 两边都是标量 `BufferLoad` 时，直接 `return tir.BufferStore(dst.buffer, src, dst.indices)`，不发出 copy intrin。

否则断言至少一侧有 extent，把缺失侧补成全 1，再调用 `legalize_pairwise_extents` 做尾部对齐广播，最后用 `to_buffer_region` 把 src/dst 编码成 `tl.region` 调用：

[tilelang/language/copy_op.py:L83-L93](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L83-L93) —— 广播对齐 + 编码 region：`legalize_pairwise_extents` 后 `to_buffer_region(..., "r"/"w", extents=...)`。

注解（`coalesced_width/disable_tma/eviction_policy`）被收进一个 `ann` 字典，其中 `eviction_policy` 的字符串被映射成整数（`evict_normal=0 / evict_first=1 / evict_last=2`）：

[tilelang/language/copy_op.py:L96-L105](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L96-L105) —— 构造 `annotations`，单参数优先级低于显式传入的 `annotations`。

最终发出 intrin——注意 op 名是 **`tl.tileop.copy`**，位置参数只有 `src, dst` 两个 region，控制信息全部走 `annotations`：

[tilelang/language/copy_op.py:L107](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L107) —— `tir.call_intrin("handle", tir.op.Op.get("tl.tileop.copy"), src, dst, annotations=ann if ann else None)`。

> **对照旧版**：`copy.py` 的同名函数发出的是 `tl.copy`（见 [tilelang/language/copy.py:L88](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy.py#L88)），且把 `coalesced_width/disable_tma/eviction_policy` 作为位置参数传入。但当前 `tilelang/language/__init__.py` 只导入了 `copy_op` 版本（[tilelang/language/__init__.py:L52](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/__init__.py#L52)），C++ 也只注册了 `tl.tileop.copy`（见 4.2.3）。所以 `copy.py` 是**遗留代码**，理解 `T.copy` 时请以 `copy_op.py` 为准。

辅助函数在 `utils/language.py`：

- [tilelang/utils/language.py:L161-L191](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/language.py#L161-L191) —— `get_buffer_region_from_load`：把带 `Ramp`（向量化下标）或普通下标的 `BufferLoad` 反推成一个 `BufferRegion`。
- [tilelang/utils/language.py:L194-L237](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/language.py#L194-L237) —— `to_buffer_region`：把 `Buffer`/`BufferRegion`/`BufferLoad` 编码成 `tl.region(...)` 调用（带 `access_type` 与 `extents`），这是 copy / fill / reduce 等原语共用的「区域表示」。
- [tilelang/utils/language.py:L406-L449](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/language.py#L406-L449) —— `legalize_pairwise_extents`：尾部对齐广播规则。

#### 4.1.4 代码实践

**目标**：亲手感受 `T.copy` 的切片搬运，并理解 extent 是「推导」出来的。

**操作步骤**（基于 [examples/quickstart.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py) 的骨架，示例代码）：

1. 写一个最小 elementwise kernel，把 global 的 `A` 整块搬到 shared，再搬回 global 的 `C`：

```python
# 示例代码：最小 T.copy 搬运
import tilelang
import tilelang.language as T

@tilelang.jit
def copy_kernel(N: int, BM: int):
    @T.prim_func
    def main(A: T.Tensor((N,), "float32"), C: T.Tensor((N,), "float32")):
        with T.Kernel(T.ceildiv(N, BM), threads=128) as (bx,):
            A_shared = T.alloc_shared((BM,), "float32")
            T.copy(A[bx * BM], A_shared)        # 切片搬运：extent 由 BM 推导
            T.copy(A_shared, C[bx * BM])         # shared -> global 切片
    return main
```

2. 把 `T.copy(A[bx * BM], A_shared)` 改成 `T.copy(A[bx * BM : (bx+1) * BM], A_shared)`，两者应当等价（验证 extent 推导）。
3. 再故意写一行 `T.copy(A_shared[0], A_shared[1])`，看它是否被前端「标量快速通道」处理（参考 [copy_op.py:L77-L81](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L77-L81)）。

**需要观察的现象**：第 2 步两种写法生成的 kernel 行为一致；第 3 步不会触发真正的 copy intrin，而是退化为一次赋值。

**预期结果 / 待本地验证**：用 `torch.testing.assert_close` 校验 `C == A`；标量搬运的退化行为可用 `kernel.get_kernel_source()` 查看生成代码确认。**该实践依赖 GPU 与 tilelang 安装，运行结果待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：`T.copy(A, B)` 中 `A`、`B` 都是完整 `Buffer` 但形状不同，会发生什么？
**答案**：前端会先做结构相等断言（[copy_op.py:L56-L57](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L56-L57)），形状不同会在编译期报错。

**练习 2**：为什么 `T.copy(buffer_a[i], buffer_b[i])` 不会生成 copy intrin？
**答案**：两边都是标量 `BufferLoad`、推不出 extent，命中标量快速通道，直接降级为 `tir.BufferStore`（[copy_op.py:L77-L81](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L77-L81)）。

---

### 4.2 T.copy 的指令选路与并行化搬运

#### 4.2.1 概念说明

`T.copy` 在 Python 前端只是发出一条 `tl.tileop.copy` intrin；**真正决定「用什么指令搬」发生在 C++ 的 lowering 阶段**。同一条 `T.copy`，根据 src/dst 的 scope、数据类型、形状、目标架构，会被编译成完全不同的代码：

- **global → shared**：Hopper 上优先用 **TMA**（`tma.load` / `tma.store`，分 1D 与多维两种），否则退化成普通线程级 cp.async/ldg。
- **shared → fragment**：用 **`ldmatrix`（LDSM）** 把 shared 里的数据按 8×8 矩阵片段喂给 tensor core。
- **fragment → shared**：用 **`stmatrix`（STSM）**。
- **shared.tmem → fragment**（Blackwell sm100）：用 **`tcgen05.ld`**。
- 以上都不满足时：**普通 SIMT 拷贝**——开若干并行循环，每个线程搬若干元素。

三个旋钮帮你微调：

- `disable_tma=True`（或全局 `pass_configs={"tl.disable_tma_lower": True}`）：强制不走 TMA，退回 cp.async/普通拷贝。在调试、或 TMA 不支持的形状下很有用。
- `coalesced_width=...`：影响普通拷贝循环的最内层合并访问宽度（一种访存合并提示）。
- `eviction_policy="evict_first"|"evict_last"|"evict_normal"`：L2 缓存逐出策略，控制这块数据在 L2 里留多久。

#### 4.2.2 核心流程

lowering 的核心是一个**优先级判定函数 `GetCopyInst`**，它按固定顺序逐个 `Check*`，命中谁就用谁：

```
GetCopyInst(target, disable_tma_lower, ...):
  if !disable_tma && !oob && CheckBulkLoad1D(...):   return kBulkLoad1D   # TMA 1D 读
  if !disable_tma && !oob && CheckBulkStore1D(...):  return kBulkStore1D  # TMA 1D 写
  if !disable_tma && CheckBulkLoad(...):             return kBulkLoad     # TMA 多维读
  if !disable_tma && CheckBulkStore(...):            return kBulkStore    # TMA 多维写
  if CheckLDSMCopy(target):   return kLDSM          # ldmatrix
  if CheckSTSMCopy(target):   return kSTSM          # stmatrix
  if CheckTMemLoad(target):   return kTMemLoad      # tcgen05.ld
  if CheckTMemStore(target):  return kTMemStore     # tcgen05.st
  return kNormal                                     # 普通 SIMT 拷贝
```

以 TMA Bulk Load 为例，`CheckBulkLoad` 要求：架构支持 bulk copy、src 在 `global` 且 dst 在 `shared`/`shared.dyn`、src 最后一维 × 每元素字节数是 16 的倍数、src 与 dst 数据类型相同。任一不满足就 fallback。

普通拷贝路径 `LowerNormalCopy` 则会先用 `MakeSIMTLoop` 生成一组**并行循环**（`ForKind::kParallel`），再交给 `LowerParallelLoop` 做线程划分、向量化与谓词保护。

#### 4.2.3 源码精读

C++ 算子注册与 builder 挂载靠宏 `TIR_REGISTER_TL_TILE_OP`，它把名字拼成 `tl.tileop.<OpName>` 并把 builder 绑到 `(args, annotations)` 构造：

[src/op/operator.h:L101-L112](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/operator.h#L101-L112) —— 宏展开为 `Op::Get("tl.tileop." #OpName)` 与 `TVM_REGISTER_OP("tl.tileop." #OpName)`，builder 调用 `Entry(args, annotations)`。

`Copy` 构造函数读取 `args[0]/args[1]` 作为 src/dst 的 region，并把 `annotations` 整个存下来（这就是前端那个 `ann` 字典）：

[src/op/copy.cc:L106-L120](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L106-L120) —— `Copy::Copy`：把两个参数 `NormalizeToBufferRegion` 成 src/dst 的 `Buffer + Range`，并保存 annotations。

注解的字段含义与读取方式定义在头文件：

[src/op/copy.h:L114-L123](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.h#L114-L123) —— `CopyNode` 字段与支持的注解键（`coalesced_width/disable_tma/eviction_policy`）。
[src/op/copy.h:L140-L156](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.h#L140-L156) —— `GetDisableTMA()` / `GetEvictionPolicy()` 读取注解。

选路优先级函数（注意 1D TMA 因为不支持越界访问，会额外要求 `!buffer_oob`）：

[src/op/copy.cc:L688-L719](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L688-L719) —— `GetCopyInst`：按 BulkLoad1D → BulkStore1D → BulkLoad → BulkStore → LDSM → STSM → TMemLoad → TMemStore → Normal 的优先级返回 `CopyInst`。`CopyInst` 枚举见 [src/op/copy.h:L17-L29](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.h#L17-L29)。

TMA Bulk Load 的前置条件（global→shared、末维对齐 16 字节、dtype 相同）：

[src/op/copy.cc:L510-L543](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L510-L543) —— `CheckBulkLoad`：架构、scope、末维 16 字节对齐、dtype 四项检查；不满足则打印 WARNING 并返回 false（fallback 到普通拷贝）。

`Lower` 根据选出的 `CopyInst` 分派到具体的 lowering 函数：

[src/op/copy.cc:L723-L755](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L723-L755) —— `CopyNode::Lower`：按 `copy_inst` 分派到 `LowerBulkCopy / LowerBulkCopy1D / LowerLDSMCopy / LowerTmemCopy / LowerNormalCopy`。

普通 SIMT 拷贝的循环生成：`MakeSIMTLoop` 选「scope 层级更低的一侧」作为循环基准，为每维 extent>1 的轴生成一个迭代变量，最内层带 `kParallel`，并把 `coalesced_width` 作为循环注解传下去：

[src/op/copy.cc:L281-L325](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L281-L325) —— `MakeSIMTLoop`：选择 base ranges、构造谓词、生成嵌套 `kParallel` 循环；其中 [src/op/copy.cc:L316-L323](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L316-L323) 把 `coalesced_width` 写进循环 annotation。

注册语句确认 op 名：

[src/op/copy.cc:L1770-L1773](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L1770-L1773) —— `TIR_REGISTER_TL_TILE_OP(Copy, copy)`，即 `tl.tileop.copy`（与前端 [copy_op.py:L107](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L107) 一致）。

#### 4.2.4 代码实践

**目标**：用 `disable_tma` 旋钮强制关闭 TMA，对比生成代码与性能，直观感受「同一条 `T.copy` 走不同指令」。

**操作步骤**（示例代码，参考真实用例 [examples/distributed/example_post_attn_all2all_transpose.py:L200](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_post_attn_all2all_transpose.py#L200)）：

1. 写一个 matmul kernel（沿用 quickstart 思路），其 `T.copy(A[...], A_shared)` 是 global→shared 搬运。
2. 分别用两种配置编译：

```python
# 示例代码：开关 TMA 对比
kernel_tma   = tilelang.compile(func)                                  # 默认（Hopper 上走 TMA）
kernel_notma = tilelang.compile(func, pass_configs={"tl.disable_tma_lower": True})
```

3. 用 `kernel_tma.get_kernel_source()` 与 `kernel_notma.get_kernel_source()` 对比生成的 CUDA 源码。
4. 用 `kernel.get_profiler().do_bench()` 测两者延迟。

**需要观察的现象**：默认版生成的源码里应出现 TMA 描述符创建与 `tma.load`（或等价 PTX）；`disable_tma_lower=True` 版应退化为线程级 cp.async / 普通加载循环，且通常更慢。

**预期结果 / 待本地验证**：在 Hopper（sm90）上 TMA 路径通常更快、线程占用更低；在非 Hopper 架构上两者可能都走普通拷贝。**运行结果待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：为什么 1D TMA（`BulkLoad1D`）比多维 TMA 多了一个 `!buffer_oob` 前置条件？
**答案**：1D TMA 不能处理越界访问（见 [copy.cc:L698-L703](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L698-L703) 的注释），存在潜在越界时会回退到带谓词的多维 TMA 或普通拷贝。

**练习 2**：`MakeSIMTLoop` 为什么在 src/dst 之间选「scope 层级更低的一侧」作为循环基准？
**答案**：选更低层级（如 fragment/shared）的一侧作为循环轴来源，可以保证生成的迭代域覆盖真正需要遍历的元素维度，避免在更高层级（如 global）的多余维度上产生过小或不对齐的循环（见 [copy.cc:L132-L148](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L132-L148) 的 scope_level 判定）。

---

### 4.3 T.view / T.reshape：零拷贝视图与布局/类型重解释

#### 4.3.1 概念说明

`T.view` 和 `T.reshape` 做的事情，用一句话讲就是：**同一块内存，换一种「形状 + 数据类型」的解释，但不搬动任何数据**。

它返回的新 `T.Tensor` 共用源 buffer 的底层指针 `src.data`，只是声明了新的 `shape`（以及 `view` 还可换 `dtype`）。正因如此，必须满足一条硬约束——**比特总数守恒**：

\[
\text{bits} = \left(\prod_{i} \text{shape}_i\right) \times \text{dtype.bits}
\]

也就是说，新视图的「元素总数 × 每元素比特数」必须等于源 buffer 的同一量。比如 16 个 `float16`（共 256 bit）可以 `view` 成 8 个 `float32`，但不能 view 成 12 个 `float16`（192 bit ≠ 256 bit）。

两者差别很小：

- `T.reshape(src, shape)`：只能换 `shape`，`dtype` 沿用源。
- `T.view(src, shape=None, dtype=None)`：`shape`、`dtype` 都可省略（省略则沿用源），可同时换 `shape` 和 `dtype`。

> **重要**：`view`/`reshape` 只是把连续内存按新形状/类型重新切分，**它不会做矩阵转置**。把 `(M,K)` 的连续内存 `view` 成 `(K,M)`，得到的是「同样的扁平序列按 (K,M) 行优先重新分组」，**而不是**数学上的转置（转置需要交换两个轴的步长，那要用带 strides 的视图，见 4.3.4 的进阶说明）。

#### 4.3.2 核心流程

`view`/`reshape` 的实现极其简短：

1. 处理默认值：`view` 的 `shape=None` 用源 `shape`、`dtype=None` 用源 `dtype`；`reshape` 的 `dtype` 恒为源 `dtype`。
2. 检查比特守恒：用 `bits_product(new_shape, new_dtype)` 与 `bits_product(src.shape, src.dtype)` 做结构相等断言。
3. 返回 `T.Tensor(new_shape, new_dtype, src.data)`——复用源指针，由 `TensorProxy` 按新形状重新算出连续 row-major strides。

#### 4.3.3 源码精读

`reshape` 与 `view` 的定义在 `customize.py`：

[tilelang/language/customize.py:L40-L53](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/customize.py#L40-L53) —— `reshape(src, shape)`：比特守恒断言后 `return T.Tensor(shape, src.dtype, src.data)`。
[tilelang/language/customize.py:L56-L66](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/customize.py#L56-L66) —— `view(src, shape=None, dtype=None)`：处理默认值、比特守恒断言后 `return T.Tensor(shape, dtype, src.data)`。

比特守恒的判定函数：

[tilelang/utils/language.py:L377-L385](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/language.py#L377-L385) —— `bits_product(shape, dtype)`：返回 \(\prod \text{shape}_i \times \text{dtype.bits}\)。

`T.Tensor(...)` 复用 `data` 并按新形状构造连续 strides：

[tilelang/language/proxy.py:L151-L154](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L151-L154) —— `TensorProxy.__call__`：标量 shape 归一化后调用父类，把 `data=data` 透传，并由 `_construct_strides` 算出连续 strides。
[tilelang/language/proxy.py:L143-L149](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L143-L149) —— `_construct_strides`：按 row-major 从末维起累乘得到 strides。

真实用例（对照阅读，帮你理解为什么需要 view）：

- **类型 + 形状重解释（shared tile）**：[examples/dsa_sparse_finetune/sparse_mla_bwd.py:L165-L166](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/dsa_sparse_finetune/sparse_mla_bwd.py#L165-L166) —— `acc_dkv_shared = T.view(KV_shared, shape=[BS // split_store, D], dtype=accum_dtype)`，把一块 shared 缓存按累加精度（accum_dtype）重新看待。
- **为 GEMM 重排权重（global，等价 reshape）**：[examples/convolution/example_convolution.py:L48-L49](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/convolution/example_convolution.py#L48-L49) —— `kernel_flat = T.Tensor((KH * KW * C, F), dtype, kernel.data)`，把 4D 卷积核 `(KH,KW,C,F)` 当成 2D 矩阵 `(KH*KW*C, F)` 喂给 `T.gemm`。这正是 `reshape` 的典型用途：**在调用 gemm/copy 之前，把张量整理成算子期望的形状**。

`view`/`reshape` 的导出在 [tilelang/language/__init__.py:L76-L89](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/__init__.py#L76-L89)（连同 `dp4a/clamp/atomic_*` 等）。

#### 4.3.4 代码实践

**目标**：用 `T.copy` 分块加载一个 `(M,K)` 矩阵到 shared，再用 `T.view` 对这块 shared tile 做形状/类型重解释并验证「比特守恒、零拷贝」；同时亲手确认 `T.view` **不能**用来做转置。

**操作步骤**（示例代码）：

1. 写一个 kernel：global `A (M,K)` → shared `A_shared (BM,BK)`，然后：

```python
# 示例代码：T.view 重解释
A_shared = T.alloc_shared((BM, BK), "float16")
T.copy(A[bx * BM, by * BK], A_shared)

# (a) 形状重解释：(BM, BK) -> (BM*BK,)，元素一一对应
A_flat = T.view(A_shared, shape=[BM * BK])

# (b) 类型重解释：BM*BK 个 float16 -> (BM*BK//2) 个 float32（比特守恒）
A_f32 = T.view(A_shared, shape=[BM * BK // 2], dtype="float32")
```

2. **转置实验**（关键）：尝试 `A_T = T.view(A_shared, shape=[BK, BM])`，然后写一段校验：在 host 上构造 `A`，运行只含「copy + view」的 kernel，比较 `A_T[j,i]` 与期望的 `A[i,j]`。

**需要观察的现象**：
- (a)(b) 中 `A_flat[i*BK+j]` 应严格等于 `A_shared[i,j]`；`A_f32` 的值是两个相邻 `float16` 比特按 `float32` 解释的结果（比特重排，零拷贝）。
- 转置实验中 `A_T[j,i]` **不等于** `A[i,j]`。因为 `view` 只是按 `(BK,BM)` 的连续 row-major 重新切分同一块扁平内存：`A_T[j,i]` 读的是偏移 `j*BM+i`，而真正的转置 `A[i,j]` 在偏移 `i*BK+j`。

**预期结果 / 待本地验证**：转置实验会「失败」（结果不是转置），这正是要建立的认知。**真正做转置**有两种正确写法：①用带 strides 的视图 `T.StridedTensor((BK, BM), (1, BM), dtype)`（[proxy.py:L157-L166](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L157-L166)，步长 `(1, BM)` 即转置）；②最常用的是在 `T.Parallel` 循环里交换下标 `C_shared[j,i] = A_shared[i,j]`，或直接用 `T.gemm` 的转置参数。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：能否把 `(4,4)` 的 `float16` buffer `view` 成 `(4,4)` 的 `float32`？
**答案**：不能。前者 4·4·16=256 bit，后者 4·4·32=512 bit，比特总数不等，`bits_product` 断言失败（[customize.py:L65](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/customize.py#L65)）。能 view 成的 `float32` 形状最多是 `(4,2)`（4·2·32=256 bit）。

**练习 2**：`T.view` 返回的新 buffer 和源 buffer 共享什么？为什么不产生拷贝？
**答案**：共享底层指针 `src.data`（[customize.py:L66](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/customize.py#L66)）。新 `T.Tensor` 只是把同一个指针配上新 shape/strides，没有任何数据搬运，所以是「零拷贝」。

---

### 4.4 c2d_im2col：卷积专用的 im2col 搬运

#### 4.4.1 概念说明

卷积有一种经典的高效实现——**im2col**：把输入图像里每个输出位置对应的感受野 patch 展开成矩阵的一列，于是卷积就变成了一个大矩阵乘法。这个「展开」过程如果用普通线程循环来做，访存很碎、很慢。

`T.c2d_im2col(img, col, nhw_step, c_step, kernel, stride, dilation, pad)` 就是 TileLang 提供的**专用 im2col 搬运原语**：在 Hopper 上，它直接落到硬件的 **TMA-im2col** 指令（`tma.load` 的 im2col 变体），让 TMA 引擎替你完成 patch 展开与边界（padding）填充，直接写进 shared memory，供后续 `T.gemm` 使用。

#### 4.4.2 核心流程

前端 `c2d_im2col` 把 `img`/`col` 编码成 region，再把卷积参数（kernel/stride/dilation/pad）与步进（`nhw_step`/`c_step`）原样传给 `tl.tileop.c2d_im2col` intrin。C++ 的 `Conv2DIm2ColOp::Lower` 在 Hopper 上构建一个 `TMAIm2ColDesc`（含 `lower_corner/upper_corner` 表达 padding、`smem_box_pixel/smem_box_channel` 表达输出 tile 形状），生成 `create_tma_im2col_descriptor` + `tma_load_im2col` 调用。

#### 4.4.3 源码精读

前端实现（注意它要求 `img` 是完整 `Buffer`、`col` 也是完整 `Buffer`，范围由 buffer 自身形状决定）：

[tilelang/language/copy_op.py:L110-L120](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L110-L120) —— `c2d_im2col` 签名。
[tilelang/language/copy_op.py:L136-L154](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L136-L154) —— 把 `eviction_policy` 映射成整数、`to_buffer_region` 编码 img/col，发出 `tl.tileop.c2d_im2col`。

C++ lowering（仅 Hopper，要求 src 在 global、dst 在 shared、src 4D、dst 2D、dtype 相同）：

[src/op/copy.cc:L1564-L1580](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L1564-L1580) —— `Conv2DIm2ColOp` 构造：解析 src/dst region 与各卷积参数。
[src/op/copy.cc:L1589-L1711](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L1589-L1711) —— `Conv2DIm2ColOpNode::Lower`：构建 `TMAIm2ColDesc`、计算各维坐标与 image_offset、生成 `tma_load_im2col`。
[src/op/copy.cc:L1786-L1789](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L1786-L1789) —— 注册为 `tl.tileop.c2d_im2col`。

真实用例对照：

[examples/convolution/example_convolution.py:L48-L67](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/convolution/example_convolution.py#L48-L67) —— Hopper 分支用 `T.c2d_im2col(data, data_shared, by, k_iter, KH, S, D, P)`，非 Hopper 分支则退化成手写的 `T.Parallel` im2col 循环；随后 `T.copy(kernel_flat[...], kernel_shared)` + `T.gemm(data_shared, kernel_shared, out_local)`。注意这里 `kernel_flat = T.Tensor((KH*KW*C, F), dtype, kernel.data)` 正是 4.3 讲的 reshape 用法。

#### 4.4.4 代码实践

**目标**：通过真实卷积示例，把 `c2d_im2col` + `T.copy` + `T.gemm` 三者串起来理解。

**操作步骤**：

1. 阅读 [examples/convolution/example_convolution.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/convolution/example_convolution.py) 的 `convolution(...)` 函数（L28-L69）。
2. 运行它：`python examples/convolution/example_convolution.py`（按文件 `main` 里的 argparse 调参）。
3. 在 Hopper 与非 Hopper 机器上各跑一次，对比是否走了 `c2d_im2col` 分支（由 `is_hopper = check_hopper()` 决定，L34）。

**需要观察的现象**：Hopper 上 kernel 源码里应出现 `tma_load_im2col`；非 Hopper 上是显式的 `T.Parallel` patch 展开循环。

**预期结果 / 待本地验证**：两种路径输出都应与参考卷积结果一致（脚本内自带校验）。运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`c2d_im2col` 为什么对 src 要求 4D、dst 要求 2D？
**答案**：im2col 把 4D 输入 `(N,H,W,C)` 的 patch 展平成 2D 矩阵 `(patch数, KH*KW*C)`，dst 的两维正好对应「输出像素」与「展开后的通道×核」维度（见 [copy.cc:L1592-L1596](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L1592-L1596) 的断言）。

**练习 2**：为什么非 Hopper 架构不能用 `T.c2d_im2col`？
**答案**：它的 lowering 只在 Hopper 上实现（`ICHECK(TargetIsHopper(...))`，[copy.cc:L1591](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L1591)），依赖 TMA-im2col 指令；其它架构需用手写循环展开（见卷积示例 L56-L62）。

## 5. 综合实践

设计一个把本讲三个知识点（`T.copy` 搬运、`T.view` 重解释、`c2d_im2col` 的思想）串起来的小 kernel：**实现一个带 reshape 的转置搬运 kernel**。

任务：给定 global `A (M, K)`，输出 `C (K, M) = A^T`。要求：

1. 用 `T.copy` 把 `(BM, BK)` 的 tile 从 global 分块加载到 `A_shared`。
2. 用 `T.view` 把 `A_shared (BM, BK)` 重解释成一个一维视图 `A_flat (BM*BK,)`，并在一个 `T.Parallel` 循环里验证 `A_flat[i*BK + j]` 与 `A_shared[i, j]` 指向同一元素（理解零拷贝）。
3. 用「交换下标」的方式完成真正的转置：`C_shared[j, i] = A_shared[i, j]`，再用 `T.copy` 把 `C_shared` 写回 global `C`。
4. 编译运行，用 PyTorch 的 `A.t()` 作参考校验。

参考骨架（示例代码）：

```python
# 示例代码：综合实践——copy + view + 转置
import tilelang
import tilelang.language as T

@tilelang.jit
def transpose_kernel(M: int, K: int, BM: int, BK: int):
    @T.prim_func
    def main(A: T.Tensor((M, K), "float16"), C: T.Tensor((K, M), "float16")):
        with T.Kernel(T.ceildiv(M, BM), T.ceildiv(K, BK), threads=128) as (bx, by):
            A_shared = T.alloc_shared((BM, BK), "float16")
            C_shared = T.alloc_shared((BK, BM), "float16")
            # (1) 切片搬运 global -> shared
            T.copy(A[bx * BM, by * BK], A_shared)
            # (2) view 重解释（零拷贝）：(BM,BK) -> (BM*BK,)
            A_flat = T.view(A_shared, shape=[BM * BK])
            # (3) 真正的转置靠交换下标，不是靠 view
            for i, j in T.Parallel(BM, BK):
                C_shared[j, i] = A_shared[i, j]
            # 顺手验证 view 的等价性（仅作演示，可删）
            # assert A_flat[i * BK + j] == A_shared[i, j]
            # (4) shared -> global 切片搬运
            T.copy(C_shared, C[by * BK, bx * BM])
    return main
```

**验收要点**：
- `C` 与 `A.t()` 数值一致（`torch.testing.assert_close`）。
- 能说清第 (3) 步为什么不能用 `T.view(A_shared, shape=[BK, BM])` 替代（见 4.3.4 的转置实验结论）。
- 用 `kernel.get_profiler().do_bench()` 量一下延迟，并尝试 `pass_configs={"tl.disable_tma_lower": True}` 看 TMA 开关对性能的影响。

> 该实践依赖 GPU 与 tilelang 安装，具体数值与性能数据待本地验证。

## 6. 本讲小结

- `T.copy(src, dst, ...)` 的搬运范围是**推导**出来的：`Buffer` 用 shape、切片用切片长度、缺一侧则广播对齐；标量搬运直接退化为一次赋值；最终发出 `tl.tileop.copy` intrin（活跃实现在 `copy_op.py`，旧版 `copy.py` 已废弃）。
- 同一条 `T.copy` 在 C++ lowering 阶段按**优先级**被自动选路：TMA（1D→多维）→ LDSM/STSM → tcgen05 → 普通 SIMT 拷贝；`disable_tma`/`coalesced_width`/`eviction_policy` 是三个可控旋钮。
- `T.view` / `T.reshape` 是**零拷贝视图**：复用 `src.data`，只换 shape（view 还可换 dtype），受「比特总数守恒」约束；它**不做转置**，转置要用交换下标或带 strides 的 `T.StridedTensor`。
- `T.c2d_im2col` 是卷积 im2col 的专用搬运，Hopper 上直接落到 TMA-im2col 指令，配合 `T.reshape`（把卷积核压成 2D）与 `T.gemm` 完成卷积。
- 区域表示（`to_buffer_region` / `tl.region`）与注解字典是 copy/fill/reduce 等多个原语共用的底层机制，理解它有助于后续阅读 u3 的 `LowerTileOp`。

## 7. 下一步学习建议

- **进入编译流水线**：本讲的 `tl.tileop.copy` 是在 [src/transform/lower_tile_op.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_tile_op.cc) 里被 `ParseOperator` 识别并调用 `CopyNode::Lower` 的——下一讲 [u3-l1 编译总览](./u3-l1-compile-overview.md) 与 [u3-l3 LowerAndLegalize](./u3-l3-lower-legalize.md) 会把这条链路讲透。
- **深入 TMA 与软件流水**：想真正搞懂 TMA 描述符、swizzle、`tma_load` 的 mbarrier 同步，可读 [u4-l2 软件流水线与异步拷贝](./u4-l2-software-pipeline.md)，那里会展开 `inject_tma_barrier`、`inject_ptx_async_copy` 等 pass。
- **布局（Layout）系统**：`T.copy` 的 TMA/LDSM 选路高度依赖 `LayoutInference` 推出的 fragment/shared 布局，建议接着读 [u4-l1 Layout 推理机制](./u4-l1-layout-inference.md)。
- **远程搬运（分布式）**：如果你关心多 GPU 间的 `put`/`get`（也是 `tl.tileop.put/get`），那是 [u6 分布式编程](./u6-l1-distributed-overview.md) 的内容，本讲只覆盖单机搬运。

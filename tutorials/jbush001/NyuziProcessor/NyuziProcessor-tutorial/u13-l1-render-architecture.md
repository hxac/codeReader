# tile-based 渲染架构

## 1. 本讲目标

本讲是「图形渲染管线」单元的第一篇，承接 [u9-l3 librender 渲染库基础](u9-l3-librender-basics.md)。在 u9-l3 里我们认识了 librender 的三件套——Surface、Texture/mipmap、RenderContext，并知道 `RenderContext::finish()` 会把渲染拆成几何阶段与像素阶段。本讲要打开 `finish()` 这个黑盒，回答四个问题：

1. 为什么 librender 选择 **tile-based（分块）/ sort-middle（中间排序）** 架构，而不是逐三角形立即渲染？
2. 几何阶段到底做了哪些事，把一个个三角形「装」进了哪里？
3. 像素阶段如何把三角形「取」出来重新渲染，并保证渲染顺序正确？
4. tile 队列这种数据结构，如何在多线程并发写入的同时还保证最终顺序？

学完后，你应当能画出 librender 的两阶段流水线图，能解释「按 tile 渲染减少外部内存带宽、免像素顺序锁」两条优势的根因，并能读懂 `finish` → `shadeVertices` → `setUpTriangle`/`enqueueTriangle` → `fillTile` 这条主调用链。

## 2. 前置知识

阅读本讲前，建议先掌握以下概念（前序讲义已建立）：

- **16 通道 SIMD 与 vector_t**：Nyuzi 一条向量指令同时处理 16 个数据通道（见 [u2-l1](u2-l1-isa-overview.md)）。librender 把一个 4×4 像素块正好映射到 16 个通道，一条 gather/scatter 指令读写整块。
- **多线程与 parallel_execute**：Nyuzi 每核 4 个硬件线程，`parallel_execute(func, ctx, N)` 把 N 个任务放进共享计数器，所有线程自取执行，全部完成后才返回（[u9-l2](u9-l2-libos-schedule-parallel.md)）。这是本讲两阶段并行的基础。
- **L2 缓存容量**：默认配置下 L2 为 **128 KiB**、缓存行 **64 字节**（见 [u3-l3](u3-l3-config-synthesis.md)、[u6-l3](u6-l3-l2-cache.md)）。这是「tile 放得进 L2」这条优势的数字依据。
- **LL/SC 与 membar**：tile 队列的并发追加用 CAS 实现（[u10-l1](u10-l1-sync-load-store.md)），`dflush` 等缓存回写指令依赖内存排序语义。

下面用一个表把本讲要反复用到的几个常量列清楚（来自 `Surface.h`）：

| 常量 | 值 | 含义 |
|------|-----|------|
| `kCacheLineSize` | 64 | 缓存行字节数，等于向量寄存器宽度 |
| `kTileSize` | 64 | 一个 tile 的边长（像素），即 64×64 |
| `kVectorSize` | 64 | 向量宽度（字节）= 16 通道 × 4 字节 |

由此可算出关键数字：一个 64×64 的 tile，若按 RGBA8888（4 字节/像素）存储，颜色缓冲占 \(64 \times 64 \times 4 = 16384\) 字节 = **16 KiB**；深度缓冲同样约 16 KiB。两者合计约 **32 KiB**，远小于 128 KiB 的 L2——这正是 tile 能「常驻 L2」的根因。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `software/libs/librender/RenderContext.h` | `RenderContext` 类声明，定义 `Triangle` 结构、tile 数组类型与所有渲染阶段的私有方法 |
| `software/libs/librender/RenderContext.cpp` | 本讲主角。命令录制、`finish()` 两阶段调度、几何阶段（`shadeVertices`/`setUpTriangle`/`enqueueTriangle`）、像素阶段（`fillTile`/`wireframeTile`） |
| `software/libs/librender/Surface.h` / `Surface.cpp` | tile 尺寸常量、`clearTile`（快速清块）、`flushTile`（用 `dflush` 把 tile 从 L2 推回主存） |
| `software/libs/librender/CommandQueue.h` | tile 队列的模板类：并发 `append`（CAS）与 `sort`（按序号重排） |
| `software/libs/librender/README.md` | 官方对架构的描述，明确点出 sort-middle、免锁、L2 友好三条特性 |
| `software/libs/libos/schedule.h` | `parallel_execute` 原型 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：**tile 架构** → **几何阶段** → **像素阶段** → **tile 队列**。前三者对应渲染管线的纵向流程，第四个是支撑并发与重排的横向数据结构。

### 4.1 tile 架构：sort-middle 是什么

#### 4.1.1 概念说明

并行图形渲染按「在哪一步把工作分发给多个处理单元」分为三类：

- **sort-first**：在管线最前端（几何之前）按屏幕区域把整块工作切给不同处理器，各自跑完整管线。
- **sort-middle**：在管线**中间**（几何阶段之后、光栅化之前）按屏幕空间区域把三角形**重新分发**。几何阶段算完所有三角形后，按三角形覆盖的屏幕区域把它们「归类」到不同桶里，光栅化阶段各处理器只处理自己那一桶。
- **sort-last**：在管线最末端，每个处理器独立渲染一整帧再合并。

librender 采用 **sort-middle / tile-based**：它把输出画面切成固定大小的方块（tile，64×64 像素），几何阶段把每个三角形塞进它可能覆盖的所有 tile 的队列里，像素阶段再逐 tile 取出队列渲染。README 把这话说得很直白：

[software/libs/librender/README.md:8-16](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/README.md#L8-L16) 中文说明：这是基于 tile 的渲染器，又称 sort-middle 架构；它把目标切成固定大小的矩形，线程把每个 tile 完全渲染完再移到下一个；两条优势是「每个线程独占一个 tile，因此无需锁即可保持像素顺序」与「正被渲染的 tile 能装进 L2，从而降低外部内存带宽」。

#### 4.1.2 核心流程

tile 网格在绑定渲染目标时就算好了。`bindTarget` 根据帧缓冲宽高与 `kTileSize` 求出列数与行数：

```
fTileColumns = ceil(fFbWidth  / 64)
fTileRows    = ceil(fFbHeight / 64)
```

一个画面被切成 `fTileColumns × fTileRows` 个 tile。整个渲染的时间线是：

```
录制阶段(单线程, 线程0)
   bindTarget / bindShader / bindVertexAttrs / drawElements ...
        │  (命令只进 fDrawQueue，不渲染)
        ▼
finish()  ──► 几何阶段(并行) ──► 像素阶段(并行)
              顶点着色              逐 tile 渲染
              三角形 setup          + 排序重放
              分桶进 tile 队列
```

关键点：**录制与执行是分离的**。`drawElements` 只把当前状态追加进 `fDrawQueue`，真正干活的是 `finish()`。这种「先录制命令、后批量回放」的模式让 librender 能在 `finish()` 时一次性看到全部图元，从而做整帧的 tile 分桶。

#### 4.1.3 源码精读

命令录制——`drawElements` 把当前状态（顶点属性、shader、索引等）压入绘制队列，本身不渲染：

[software/libs/librender/RenderContext.cpp:72-76](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L72-L76) 中文说明：把索引缓冲记入当前状态，并把该状态追加到绘制队列 `fDrawQueue`。

tile 网格计算——`bindTarget` 求列数行数：

[software/libs/librender/RenderContext.cpp:57-64](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L57-L64) 中文说明：记录渲染目标，并由帧缓冲宽高向上取整算出 tile 的列数 `fTileColumns` 与行数 `fTileRows`。

tile 尺寸与缓存行常量：

[software/libs/librender/Surface.h:29-33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.h#L29-L33) 中文说明：定义缓存行 64 字节、tile 边长 64 像素、向量宽 64 字节，并用静态断言强制 tile 边长是 4 的幂（便于递归细分到 4×4）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「一个 tile 的工作集放得进 L2」。

**操作步骤**：

1. 打开 `Surface.h`，确认 `kTileSize = 64`、`kCacheLineSize = 64`。
2. 回忆默认 L2 = 128 KiB（`config.svh` 中 `L2_SETS/L2_WAYS`，见 [u3-l3](u3-l3-config-synthesis.md)）。
3. 计算一个 tile 的颜色缓冲与深度缓冲字节数。

**需要观察的现象 / 预期结果**：

- 颜色缓冲：\(64 \times 64 \times 4 = 16384\) B = 16 KiB。
- 深度缓冲：同样约 16 KiB。
- 合计 ≈ 32 KiB，占 128 KiB L2 的 1/4，**单个 tile 完全驻留 L2 仍有富余**。这正是后续像素阶段可大量命中 L2 的前提。

> 待本地验证：若你在 `config.svh` 改了 L2 容量或 tile 尺寸，请重新代入上面公式。

#### 4.1.5 小练习与答案

**练习 1**：把 `kTileSize` 改成 128，单个 tile 的颜色+深度缓冲要占多少？还能保证放得进默认 128 KiB L2 吗？

**答案**：\(128 \times 128 \times 4 \times 2 = 131072\) B = 128 KiB，恰好把 L2 撑满，几乎没有空间留给三角形参数等其它数据，故 64 是更稳妥的取值。

**练习 2**：sort-middle 与 sort-last 的「排序点」分别在哪？

**答案**：sort-middle 在几何与光栅化之间按屏幕区域排序分发；sort-last 在管线末端，各单元各渲一整帧再合并。

---

### 4.2 几何阶段：顶点着色与三角形 setup

#### 4.2.1 概念说明

几何阶段（Geometry Phase）负责把「顶点数据」变成「准备好被光栅化的三角形，并分进各 tile 队列」。它对每个绘制命令依次做两步，且**每步全部做完才进下一步**（`parallel_execute` 是汇合屏障）：

1. **顶点着色（shadeVertices）**：对每个顶点跑顶点 shader，输出顶点参数（位置 + 颜色/纹理坐标等）。这一步是 SIMD 的：每次处理 16 个顶点，正好填满 16 个向量通道。
2. **三角形 setup（setUpTriangle）**：按索引缓冲取三个顶点组成三角形，做近平面裁剪、透视除法、背面剔除、屏幕→光栅坐标转换，最后用包围盒测试把三角形塞进它覆盖的所有 tile 队列。

注意一个设计取舍：几何阶段**完全并行**，多个线程同时跑 `setUpTriangle`，因此三角形进 tile 队列的**顺序是乱的**——这个乱序要在像素阶段靠排序修正（见 4.3、4.4）。

#### 4.2.2 核心流程

`finish()` 中几何阶段的调度伪代码：

```
for 每个绘制命令 state in fDrawQueue:
    给所有顶点分配参数缓冲 state.fVertexParams
    parallel_execute(_shadeVertices,  任务数 = ceil(顶点数/16))   // 屏障
    parallel_execute(_setUpTriangle,  任务数 = 三角形数)          // 屏障
    fBaseSequenceNumber += 本命令的三角形数
```

- `_shadeVertices` 的任务粒度是「16 个顶点」，每个任务调 `shadeVertices(index)`，用 `gatherElements` 把 16 个顶点的同一属性聚成一个向量，调 shader，再用 `scatter_storef_masked` 把 16 组参数写回。
- `_setUpTriangle` 的任务粒度是「1 个三角形」，每个任务调 `setUpTriangle(triangleIndex)`，按 `triangleIndex*3` 取三个索引。
- `fBaseSequenceNumber` 是**全局递增的三角形序号**，跨绘制命令累加，是后续排序恢复提交顺序的关键（见 4.4）。

顶点着色与三角形 setup 之间没有数据依赖问题，但因为 `setUpTriangle` 要读 `shadeVertices` 写出的参数，所以两者必须串行（用两次 `parallel_execute` 的屏障天然保证）。

#### 4.2.3 源码精读

`finish()` 主体——先建 tile 数组，再跑几何、像素两阶段：

[software/libs/librender/RenderContext.cpp:98-129](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L98-L129) 中文说明：`finish()` 先按 `fTileColumns*fTileRows` 分配 tile 数组（每 tile 一个 `TriangleArray`）；几何阶段对每个绘制命令先 `parallel_execute(_shadeVertices)`（顶点着色）、再 `parallel_execute(_setUpTriangle)`（三角形 setup），并把三角形数累加进 `fBaseSequenceNumber`；像素阶段用一次 `parallel_execute(_fillTile)` 覆盖所有 tile。

顶点着色——16 个顶点一批，gather 属性、调 shader、scatter 写参数：

[software/libs/librender/RenderContext.cpp:149-181](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L149-L181) 中文说明：`shadeVertices` 算出本批 16 个顶点的掩码，逐属性 `gatherElements` 聚成向量，交给 shader，再用步进指针向量 `scatter_storef_masked` 把 16 组参数散写到参数缓冲。

三角形 setup 入口——按近平面裁剪分派：

[software/libs/librender/RenderContext.cpp:258-311](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L258-L311) 中文说明：`setUpTriangle` 取三个顶点参数，用一个 3 位 `clipMask` 表示哪几个顶点在近平面之外，按掩码分派到「不裁剪 / 裁一个顶点(`clipOne`) / 裁两个顶点(`clipTwo`)」，被裁掉的三角形可能裂成两个。

真正完成 setup 与分桶的 `enqueueTriangle`：透视除法、坐标转换、背面剔除、包围盒、写入 tile 队列：

[software/libs/librender/RenderContext.cpp:318-396](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L318-L396) 中文说明：`enqueueTriangle` 先做透视除法（x,y 除以 w）、把屏幕空间 \([-1,1]\) 换算成光栅坐标；用叉积判定绕向并做背面剔除；算出三角形包围盒并剔除完全在视口外的；最后按包围盒求出覆盖的 tile 范围，把三角形追加进这些 tile 的队列。

其中**包围盒分桶**的核心几行（这是几何→像素的「中间排序」发生处）：

[software/libs/librender/RenderContext.cpp:385-395](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L385-L395) 中文说明：用包围盒左/右/上/下除以 `kTileSize` 得到覆盖的 tile 下标范围 `minTileX..maxTileX`、`minTileY..maxTileY`，双重循环把同一个 `Triangle` 追加到范围内每个 tile 的 `TriangleArray`。

注意：这是**保守**的包围盒测试——只要三角形包围盒碰到某个 tile 就入队，三角形实际可能并不覆盖该 tile。精确的剔除留给像素阶段的 `triangleRejected`（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：跟踪一个三角形从索引到入队的全过程，理解「序号 + 分桶」。

**操作步骤**：

1. 在 `RenderContext.cpp:258` 的 `setUpTriangle` 设 mental 断点，假设 `triangleIndex=0`、`fBaseSequenceNumber=0`。
2. 跟到 `enqueueTriangle`（L318），注意第一行 `tri.sequenceNumber = sequence`，而 `sequence` 是调用方传入的 `fBaseSequenceNumber + triangleIndex`。
3. 跟到 L387-395 的分桶循环，假设某三角形包围盒落在 tile (2,3) 到 (3,4)，则它会被 `append` 到 4 个 tile 队列：`(3*col+2)`、`(3*col+3)`、`(4*col+2)`、`(4*col+3)`。

**需要观察的现象 / 预期结果**：

- 同一个三角形会出现在**多个** tile 队列里（按包围盒覆盖的 tile 数复制）。
- 每份拷贝带着同一个 `sequenceNumber`，像素阶段据此排序。
- 若三角形完全在近平面后被 `clipOne` 裂成两个，会调用两次 `enqueueTriangle`，两个新三角形各自带**同一个** `sequence`（来自原 `fBaseSequenceNumber + triangleIndex`）——这一点对保持深度/混合顺序很关键。

> 待本地验证：可在 `enqueueTriangle` 末尾临时加一行 `printf("tri seq=%d tiles=[%d..%d, %d..%d]\n", ...)`（示例代码，非项目原有），跑一个简单场景观察输出。

#### 4.2.5 小练习与答案

**练习 1**：为什么顶点着色的任务数是 `(numVertices + 15) / 16` 而不是 `numVertices`？

**答案**：顶点着色每次处理 16 个顶点（填满 16 通道向量），任务粒度是「一批 16 个」，所以任务数要向上取整除以 16。最后一批不足 16 个时用掩码 `mask` 屏蔽多余通道（见 L153-157）。

**练习 2**：`fBaseSequenceNumber += numTriangles` 放在每条绘制命令的循环末尾，作用是什么？

**答案**：让跨命令的三角形序号**全局单调递增**。这样即使整帧有多个 `drawElements`，所有三角形共享一个全序，像素阶段一次排序就能恢复完整的提交顺序。

---

### 4.3 像素阶段：逐 tile 渲染与排序重放

#### 4.3.1 概念说明

像素阶段（Pixel Phase）在几何阶段全部完成后才开始。它把工作切成「每个 tile 一个任务」，用 `parallel_execute(_fillTile, fTileColumns*fTileRows)` 让所有线程并行渲染。**每个线程独占一个 tile**，不同线程绝不会碰同一个像素，因此：

- **无需像素顺序锁**：这是 tile 架构相对立即模式渲染器最大的并发优势——立即模式里多个三角形流水般写同一片像素，要保持 API 顺序就得加锁或串行；tile 模式把对同一像素的所有写入收拢到同一个线程内顺序执行，天然无竞争。
- **L2 友好**：一个 tile 的颜色/深度缓冲 ≈ 32 KiB，驻留 L2；线程在该 tile 内反复读写像素都命中 L2，只有 tile 渲染完才用 `dflush` 把脏行整体推回主存，外部带宽大幅降低。

每个 tile 内部，`fillTile` 依次做：清颜色缓冲（可选）→ 清深度缓冲 → **按序号排序恢复提交顺序** → 遍历该 tile 的三角形 → 精确剔除 → 用 `TriangleFiller` 光栅化 → `flushTile` 回写。

#### 4.3.2 核心流程

```
fillTile(index):                         // index 是 tile 在网格中的线性编号
    (x, y) = index 拆成 tile 列/行
    (tileX, tileY) = (x*64, y*64)        // tile 左上角的像素坐标
    若需清色: colorBuffer.clearTile(tileX, tileY, clearColor)
    若有深度: depthBuffer.clearTile(tileX, tileY, -inf)
    tile.sort()                          // ★ 按 sequenceNumber 重排，恢复提交顺序
    for tri in tile:                     // 顺序遍历（已是 API 顺序）
        if triangleRejected(tile, tri):  // 精确剔除包围盒误判
            continue
        TriangleFiller.setUpTriangle(...)   // 建插值器
        filler.setUpParam(...)              // 逐参数建透视正确插值
        fillTriangle(...)                   // 递归细分到 4x4，逐块着色+深度测试+写回
    colorBuffer.flushTile(tileX, tileY)     // 把脏行 dflush 回主存
```

排序是关键：几何阶段并行写入导致 tile 队列里三角形顺序是乱的（见 4.4 的并发 append），`tile.sort()` 在渲染前把它们按 `sequenceNumber` 排回提交顺序。这样深度测试、alpha 混合的结果才与「逐三角形立即渲染」一致。

#### 4.3.3 源码精读

`fillTile` 全貌——清、排、遍历、精确剔除、光栅化、回写：

[software/libs/librender/RenderContext.cpp:422-494](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L422-L494) 中文说明：`fillTile` 把线性 `index` 拆成 tile 坐标，按需清色/清深度，调用 `tile.sort()` 把三角形恢复成提交顺序，然后遍历每个三角形，先用 `triangleRejected` 做精确剔除（排除几何阶段包围盒测试的误判），再用 `TriangleFiller` 建插值器并光栅化，最后 `flushTile`。

精确剔除——修正包围盒的保守性：

[software/libs/librender/RenderContext.cpp:450-467](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L450-L467) 中文说明：用 `triangleRejected`（基于边的 reject corner 测试）判断三角形是否真的覆盖本 tile；若不覆盖则 `continue` 跳过，省下后续建插值器的开销。

快速清块——`clearTile` 用块写一次清一行：

[software/libs/librender/Surface.h:83-104](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.h#L83-L104) 中文说明：当 tile 满 64×64 且颜色空间合适时走快路径，每行用 4 条向量写清 64 像素，整 tile 几十次写即可清完，全部落在 L2。

回写主存——`flushTile` 用 `dflush` 把脏行推出 L2：

[software/libs/librender/Surface.cpp:154-170](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/Surface.cpp#L154-L170) 中文说明：`flushTile` 遍历 tile 内每个缓存行，对每行发一条 `dflush` 内联汇编指令，把 L2 中的脏数据推回系统内存，保证最终帧缓冲对外可见。

> 说明：`dflush` 是 Nyuzi 专用的缓存回写指令，对应硬件的 `CACHE_FLUSH` 类操作（见 [u2-l3](u2-l3-memory-instructions.md) 的缓存控制访存）。`fillTile` 在 tile 全部渲染完后调用一次，把该 tile 的所有脏行批量回写——这是「L2 友好」的落点：渲染期间数据在 L2 内反复命中，结束时一次性回写。

#### 4.3.4 代码实践

**实践目标**：体会「逐 tile 渲染 = 免锁 + L2 命中」，并验证排序的必要性。

**操作步骤**：

1. 在 `fillTile`（L422）与 `flushTile`（Surface.cpp L154）各设一个 mental 断点。
2. 想象两个三角形 A、B 都覆盖 tile (1,1)，且 A 先于 B 提交（A.seq < B.seq）。但由于几何阶段并行，B 可能先被 `append` 进 `fTiles[该 tile]`。
3. 跟踪 `tile.sort()`（L440）把它们换回 A 在前、B 在后。
4. 追踪遍历循环（L444）此时按 A、B 顺序做深度测试与写回。

**需要观察的现象 / 预期结果**：

- 若**注释掉** `tile.sort()`（示例修改，非项目原有），半透明（blend）场景会出现颜色错乱——因为后提交的三角形可能先写像素；不透明场景因有 Z-buffer 通常仍正确，但理论上仍依赖顺序。
- 整个 tile 渲染期间，颜色/深度缓冲应全部命中 L2；只有 `flushTile` 处产生对主存的写。

> 待本地验证：在开了 `enableBlend` 的场景里临时去掉 `tile.sort()`，对比帧输出是否变化。

#### 4.3.5 小练习与答案

**练习 1**：为什么「每个线程独占一个 tile」就能免除像素顺序锁？

**答案**：tile 互不重叠，任一像素只属于一个 tile，只被负责该 tile 的那一个线程写。因此不会有跨线程写同一像素的竞争，自然不需要锁。提交顺序由该线程内 `sort` 后的顺序遍历保证。

**练习 2**：`triangleRejected`（精确剔除）与几何阶段的包围盒分桶是什么关系？能否只保留一个？

**答案**：包围盒分桶是粗筛（保守，快，在几何阶段把三角形派给候选 tile）；`triangleRejected` 是精筛（精确，在像素阶段排除包围盒误判）。不能只留包围盒——它会让三角形被发到实际不覆盖的 tile 造成浪费；也不能只留精确剔除——那样几何阶段无法分桶。两者互补。

---

### 4.4 tile 队列：并发追加与排序重排

#### 4.4.1 概念说明

tile 队列（`TriangleArray`，即 `CommandQueue<Triangle, 64>`）是几何阶段与像素阶段之间的桥梁，也是 sort-middle 的「桶」。它有两个看似矛盾的需求：

1. **几何阶段多线程并发写入**：`setUpTriangle` 在多个线程同时跑，它们都会往自己三角形覆盖的那些 tile 队列里 `append`，必须线程安全。
2. **像素阶段要按提交顺序读**：光栅化对顺序敏感（深度、混合），所以渲染前要能把乱序的队列排回 `sequenceNumber` 顺序。

`CommandQueue` 用「无锁 CAS 追加 + 事后插入排序」分别满足这两点：写入快（大部分情况无锁），读前一次性排序。

#### 4.4.2 核心流程

**并发追加（append）**：队列由一串定长桶（`Bucket`，每桶 64 项）链成。每个待写项先 CAS 抢占当前桶的下一个空闲下标 `fNextBucketIndex`：

```
loop:
    index  = fNextBucketIndex          // 想写入的位置
    bucket = fLastBucket
    if 桶满 or 无桶: allocateBucket(); continue
    if CAS(fNextBucketIndex, index, index+1) 成功: break   // 抢到槽位
bucket->items[index] = value                          // 写入（注释明确：多线程下顺序任意）
```

桶满时由 `allocateBucket` 在自旋锁保护下新挂一个桶。注释明确说明：**多线程并发调用时插入顺序是任意的**——这正是像素阶段需要排序的根因。

**排序（sort）**：渲染前用插入排序按 `Triangle::operator>`（即 `sequenceNumber`）排升序。插入排序在「基本有序」时接近线性，而这里序列号本身大致递增（只是局部乱序），效率高。

**对比关系**：

| 阶段 | 调用 | 并发性 | 顺序 |
|------|------|--------|------|
| 几何 setup | `append` | 多线程并发，CAS 无锁 | 任意 |
| 像素 fill 前 | `sort` | 单线程（每个 tile 的 owner） | 恢复为 sequenceNumber 升序 |

#### 4.4.3 源码精读

`TriangleArray` 类型定义与 `Triangle` 结构（含排序用的序号与 `operator>`）：

[software/libs/librender/RenderContext.h:108-120](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.h#L108-L120) 中文说明：`Triangle` 结构包含 `sequenceNumber`、三个顶点的屏幕/光栅坐标与参数指针、绕向；`operator>` 仅比较 `sequenceNumber`，是排序的依据。

[software/libs/librender/RenderContext.h:137](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.h#L137) 中文说明：`TriangleArray` 就是 `CommandQueue<Triangle, 64>`，每个桶 64 个三角形。

并发追加——CAS 抢槽位：

[software/libs/librender/CommandQueue.h:49-74](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/CommandQueue.h#L49-L74) 中文说明：`append` 先读当前桶下标，若桶满则调 `allocateBucket` 新建桶，否则用 `__sync_bool_compare_and_swap` 原子地把 `fNextBucketIndex` 从 `index` 推进到 `index+1`，抢成功才写入；注释明确多线程并发时插入顺序任意。

排序——按序号插入排序：

[software/libs/librender/CommandQueue.h:91-110](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/CommandQueue.h#L91-L110) 中文说明：`sort` 用插入排序，借助 `operator>`（即比 `sequenceNumber`）把队列排升序；注释指出插入排序在「基本有序」时很高效，而本场景序列号大体递增、仅局部乱序，正合适。

桶分配的自旋锁（桶满时的串行化点）：

[software/libs/librender/CommandQueue.h:199-241](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/CommandQueue.h#L199-L241) 中文说明：`allocateBucket` 用自旋锁 `fSpinLock`（CAS 获取）串行化建桶，注释特意说明自旋等待时用普通读而非 CAS 读，以减少 L2 接口流量，靠一致性广播把线程踢出循环。

> 关键联系：CAS 的底层就是 [u10-l1](u10-l1-sync-load-store.md) 讲的 `load_sync`/`store_sync`（LL/SC）；而自旋锁减少 L2 流量的技巧，与 [u10-l1](u10-l1-sync-load-store.md)、[u6-l3](u6-l3-l2-cache.md) 里「snoop 广播使 L1 副本失效」的机制呼应。

#### 4.4.4 代码实践

**实践目标**：验证「并发追加产生乱序、排序恢复顺序」这一对设计。

**操作步骤**：

1. 读 `CommandQueue.h:49-74` 的 `append`，确认它对写入顺序无任何保证（注释 L46-48）。
2. 读 `RenderContext.cpp:318-322` 的 `enqueueTriangle` 开头：`tri.sequenceNumber = sequence`，确认每份三角形拷贝都带提交序号。
3. 读 `RenderContext.cpp:440` 的 `tile.sort()`，确认像素阶段遍历前先排序。

**需要观察的现象 / 预期结果**：

- 设想 4 线程同时跑 `setUpTriangle`，处理序号 0..99 的三角形。即便三角形 0、1、2、3 覆盖同一 tile，它们进入该 tile 队列的物理顺序可能是 2、0、3、1。
- 经 `sort()` 后，遍历顺序必为 0、1、2、3，与提交顺序一致。
- 结论：**「并发写乱序 + 序号 + 渲染前排序」= 并行几何 + 顺序正确的光栅化**，这正是 sort-middle 把几何与光栅化解耦的关键。

> 待本地验证：可临时在 `fillTile` 的 `tile.sort()` 前打印该 tile 前 10 个三角形的 `sequenceNumber`，对比是否非单调，再在 `sort()` 后打印确认变单调（示例调试代码）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `append` 大部分情况下不需要锁，只有桶满才上自旋锁？

**答案**：正常追加只是抢占桶内一个下标，用 CAS 即可无锁完成（CAS 失败者重试）。只有当前桶写满、需要新建桶并更新 `fLastBucket`/`fNextBucketIndex` 这一对「结构指针」时，才必须串行化，故用自旋锁保护建桶这一步。

**练习 2**：如果不用 `sequenceNumber` 而是按「物理进入队列的顺序」渲染，会发生什么？

**答案**：几何阶段是并行的，物理顺序几乎是随机的，会导致半透明混合、深度判断与「逐三角形立即渲染」的参考结果不一致，画面错误。`sequenceNumber` 把「提交顺序」这一信息显式编码进每个三角形，使排序可恢复正确顺序。

---

## 5. 综合实践

把四个模块串起来，完成下面的分析与运行任务。

**任务**：解释「为何按 tile 渲染可减少外部内存带宽并避免像素顺序锁」，并用一条调用链说明三角形如何按包围盒分配到 tile 队列、再在像素阶段排序重放。

**步骤**：

1. **带宽分析（对应 4.1、4.3）**：
   - 计算单个 64×64 tile 的颜色缓冲字节数（16 KiB），与默认 L2（128 KiB）比较，说明 tile 工作集可驻留 L2。
   - 解释立即模式渲染为何带宽高：每个三角形写散布在整帧的像素，反复在 L2 与主存间搬移缓存行；而 tile 模式下像素在 L2 内反复命中，仅在 `flushTile` 时一次性 `dflush` 回写。
   - 指出落点代码：`Surface.cpp:154-170` 的 `dflush`。

2. **免锁分析（对应 4.3）**：
   - 说明 tile 互不重叠 ⇒ 任一像素只被一个线程写 ⇒ 无需像素顺序锁。
   - 提交顺序由「序号 + `tile.sort()` + 顺序遍历」保证，落点：`RenderContext.cpp:440`。

3. **三角形生命周期（对应 4.2、4.4）**：
   - 画一条调用链：`finish()` → `parallel_execute(_setUpTriangle)` → `setUpTriangle`（裁剪 L258）→ `enqueueTriangle`（透视除法/背面剔除/包围盒 L318）→ **包围盒分桶入多个 tile 队列（L385-395）**。
   - 像素阶段：`parallel_execute(_fillTile)` → `fillTile`（L422）→ `tile.sort()`（L440）→ 精确剔除（L450）→ `TriangleFiller` 光栅化 → `flushTile`。
   - 用一句话点出 sort-middle 的「排序点」：在 `enqueueTriangle` 的包围盒分桶处（几何→像素之间按屏幕区域分发）。

4. **运行验证（可选）**：
   - 运行现成的渲染测试，例如渲染一个三角形：

     ```bash
     python3 tests/render/triangle/runtest.py
     ```

     该脚本用 `test_harness.register_render_test` 在 emulator 目标上构建并运行 `main.cpp`，再与一个固定图像哈希比对（见 `tests/render/triangle/runtest.py`）。
   - 预期：测试通过表示渲染输出与参考一致。

> 待本地验证：上述运行命令的输出取决于本机是否已按 [u1-l2](u1-l2-build-and-run.md) 装好工具链与 emulator；若未构建，先在仓库根目录 `cmake . && make`。若想看到逐 tile 的统计，可在 `RenderContext.cpp` 顶部条件编译开关 `DISPLAY_STATS`（L131-134）启用后重新构建 librender，观察 `total triangles` 与 `used ... bytes`（示例：这是项目原有代码，但需改 CMake 开关，属于「修改配置」型实践）。

## 6. 本讲小结

- librender 是 **sort-middle / tile-based** 架构：几何阶段把三角形按屏幕区域（包围盒）分进各 tile 队列，像素阶段逐 tile 取出渲染；「排序」发生在几何与光栅化之间。
- 渲染**先录制后执行**：`drawElements` 只入命令队列，`finish()` 才用两阶段 `parallel_execute` 真正干活。
- **几何阶段**两步（每步是屏障）：顶点着色（SIMD，每次 16 顶点）→ 三角形 setup（近平面裁剪、透视除法、背面剔除、坐标转换、包围盒分桶）。
- **像素阶段**：每个线程独占一个 64×64 tile，顺序为 清色/清深 → `sort` 恢复提交顺序 → 精确剔除 → `TriangleFiller` 光栅化 → `flushTile` 回写。
- 两条核心优势的根因：tile 互不重叠 ⇒ **免像素顺序锁**；tile 工作集（≈32 KiB）驻留 128 KiB L2，仅 `dflush` 时回写 ⇒ **外部内存带宽低**。
- **tile 队列**用 CAS 无锁并发 `append`（几何阶段乱序写入）+ 渲染前插入排序（按 `sequenceNumber` 恢复顺序），化解了「并行几何」与「顺序光栅化」的矛盾。

## 7. 下一步学习建议

本讲只讲了 tile 架构的「调度与分桶」，没有展开 tile 内部如何把三角形变成像素。建议接着学：

- **u13-l2 光栅化与三角形填充**：精读 `Rasterizer.cpp` 与 `TriangleFiller.cpp`，看三角形如何递归细分到 4×4 像素块、如何把 16 通道 SIMD 用于并行覆盖测试与 Z-buffer 早期剔除。这正是本讲 `fillTriangle(...)` 调用的内部实现。
- **u13-l3 着色器与纹理采样**：看顶点/像素 shader 的回调接口、透视正确参数插值（本讲 `setUpParam` 的延续），以及纹理采样与 mipmap 选择。

复习建议：回看 [u9-l3](u9-l3-librender-basics.md) 的 `Surface` 块读写与 `RenderContext::finish` 概述，能把本讲的 tile 细节与上层抽象对齐；回看 [u9-l2](u9-l2-libos-schedule-parallel.md) 的 `parallel_execute` 与 [u10-l1](u10-l1-sync-load-store.md) 的 CAS/LL-SC，能让你彻底读懂 tile 队列的并发与排序。

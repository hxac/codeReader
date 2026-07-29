# 后处理：FMA 融合、loop split 与 bytecode 输出

## 1. 本讲目标

本讲是「MLIR 转换 Pass 体系」单元的收尾篇。前面几讲（u3-l1 ~ u3-l6）讲的都是 **`convert-triton-to-cuda-tile` 之前或之中** 的事情：预处理 `map_elementwise`、控制流结构化、assume 重写、主转换本身、以及无序内存模型的 token 生成。

本讲聚焦 **主转换之后** 的三件事，学完后你应该能够：

1. 说清楚 `fuse-fma`、`loop-split`、`strip-debuginfo` 这几个 **cuda_tile 级 pass** 为什么必须「嵌套」进 `cuda_tile.module`（有的还要再嵌套进 `entry`）里运行，而不是直接挂在最外层 pass manager 上。
2. 说清楚 `write_bytecode` 如何从一整棵 MLIR 模块树里 **定位** 到那个唯一的 `cuda_tile::ModuleOp`，并把它序列化成 `tileiras` 能消费的 bytecode。
3. 说清楚 `only_contain_legal_dialects` 这道 **合法性校验** 在整个 `make_tileir` 流水线的位置、它检查什么、不通过时会发生什么。

一句话：本讲把 TileIR 编译流水线 **从主转换之后到交给外部 `tileiras` 之前** 的「清扫 + 打包 + 验收」三步讲透。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（在前置讲义中已建立）：

- **三段式流水线**（u2-l3）：`make_ttir` → `make_tileir` → `make_cubin`，本讲全部内容都发生在 `make_tileir` 的尾巴和 `make_cubin` 的开头。
- **主转换 `convert-triton-to-cuda-tile`**（u3-l1/u3-l2）：它把 `tt.*` 方言 lowering 成 `cuda_tile` 方言，并 **在 builtin `ModuleOp` 内部插入一个 `cuda_tile::ModuleOp` 容器**。本讲所有 pass 都作用在这个容器 **内部**。
- **cuda_tile 方言的合法性**（u3-l1）：`CudaTileConversionTarget` 把 `cuda_tile` 设为合法，其它方言（`triton/scf/cf/gpu/ub`）设为非法。
- **tileiras**（u2-l7）：外部 NVIDIA 编译器，只吃 `cuda_tile` 方言的 bytecode，产出 `.cubin`。

如果你对 **嵌套 pass manager（nested pass manager）** 完全陌生，记住一个直觉即可：MLIR 的 pass 可以挂在不同的「容器」层级上，挂在哪一层就只扫那一层及其子节点。本讲会反复用到这一点。

> 术语速查
> - **FMA**（Fused Multiply-Add）：把「先乘后加」\( a \times b + c \) 融合成一条指令，比两条独立指令更快且数值上更准（只舍入一次）。
> - **嵌套 pass manager**：`pm.nest<某容器Op>()` 返回一个只能在该容器内部跑 pass 的子 manager。
> - **bytecode**：MLIR 的一种紧凑二进制序列化格式（区别于人可读的 `.mlir` 文本），`tileiras` 只认这种格式。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [third_party/tileir/triton_tileir.cc](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc) | C++ pybind 插件入口。本讲的 `add_fma_fusion` / `add_loop_split` / `add_strip_debuginfo` / `write_bytecode` / `only_contain_legal_dialects` 全部在这里定义。 |
| [third_party/tileir/backend/compiler.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py) | 后端 Python。`make_tileir` 决定上述 pass **是否挂载、按什么顺序挂载**；`make_cubin` 调用 `write_bytecode`。 |
| [third_party/tileir/test/FileCheck/fma.mlir](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/fma.mlir) | fuse-fma 的 lit 测试（目前为占位文件，仅含 RUN 行）。 |
| [third_party/tileir/tools/triton-cuda-tile-opt/triton-cuda-tile-opt.cpp](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/tools/triton-cuda-tile-opt/triton-cuda-tile-opt.cpp) | 独立调试工具，注册了 fuse-fma / loop-split 两个 pass，是复现本讲 pass 行为的入口。 |

注意：`createFuseFMAPass()` 和 `createLoopSplitPass()` 这两个 pass 的 **实现并不在本仓库**，而是来自构建期克隆进来的 NVIDIA `cuda-tile` 库（见 u4-l4）。本仓库只负责 **注册并按正确方式调用** 它们。因此本讲讲解的重点是「怎么挂、挂在哪一层、什么时候挂」，而不是 pass 内部的图重写算法。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：① cuda_tile 级 pass 的嵌套方式；② bytecode 写出；③ 合法性校验收尾。

### 4.1 cuda_tile 级 pass 的嵌套方式

#### 4.1.1 概念说明

主转换 `convert-triton-to-cuda-tile` 跑完后，IR 长这样（结构示意）：

```
builtin.module                           ← 最外层 MLIR 模块（make_tileir 接收的就是它）
└── cuda_tile.module                     ← 主转换插入的「容器」
    └── cuda_tile.entry @kernel_name(...)  ← 一个 kernel（可能不止一个）
        └── ... 一堆 cuda_tile 算子（arith / load / store / dot ...）
```

也就是说，主转换产出的真正「业务 IR」**并不在 builtin 模块的直接子节点上**，而是被包在 `cuda_tile::ModuleOp` 这个容器里，再往下一层才是每个 kernel 的 `cuda_tile::EntryOp`（其文本助记符是 `experimental$func`，shell 里要写成 `cuda_tile.experimental$func`）。

这就带来一个直接问题：如果你把 `fuse-fma` 直接挂在最外层 `pm` 上，pass manager 会去扫 builtin 模块这一层，根本碰不到深处的算子。所以这类 pass 必须 **显式嵌套（nest）** 进对应的容器层级，告诉 pass manager「请下沉到这一层再开始扫」。

本模块要讲的三个 cuda_tile 级 pass，按嵌套深度可以分成两类：

| pass | 嵌套深度 | 作用层级 | 是否默认启用 |
| --- | --- | --- | --- |
| `fuse-fma` | 两层（module → entry） | 每个 kernel 内部 | 是（受 `enable_fp_fusion` 控制，默认开） |
| `loop-split` | 两层（module → entry） | 每个 kernel 内部 | **否**（已暴露但默认不挂） |
| `strip-debuginfo` | 一层（module） | 整个 cuda_tile 模块 | 是 |

注意 **loop-split 目前默认不启用** —— 这是一个容易踩坑的点，下面 4.1.3 会用源码证实。

#### 4.1.2 核心流程

主转换之后，`make_tileir` 依次挂载这些「后处理」pass（只列与本讲相关的部分）：

```
add_triton_to_cudatile(...)        # 主转换：插入 cuda_tile.module 容器（u3-l2）
add_auto_gen_memtoken(...)         # 无序内存模型 token 生成（u3-l6）
add_inliner(pm)                    # 内联
if opt.enable_fp_fusion:
    add_fma_fusion(pm)             # ← 本讲：FMA 融合，嵌套进 module→entry
add_strip_debuginfo(pm)            # ← 本讲：剥离调试信息，嵌套进 module
pm.run(mod, "make_tileir")         # 一次性跑完上面所有 pass
```

三个直觉：

1. **FMA 融合放在内联之后**：内联把函数调用展平后，乘加模式才完整暴露出来，融合机会最大。
2. **strip debuginfo 放在最后**：前面所有 pass（包括 fma）可能仍需要调试位置信息来报错或做决策，所以把「擦除调试信息」留到最末尾，确保不影响上游 pass。
3. **嵌套是 pass 注册时就写死的**：在 Python 侧调用 `tileir.passes.add_fma_fusion(pm)` 时，C++ 内部立刻执行两层 `nest`，把 pass 挂到 entry 层；Python 调用者完全感知不到这个深度。

#### 4.1.3 源码精读

先看 `add_fma_fusion` 和 `add_loop_split` —— 它们都是 **两层嵌套**：

```cpp
// triton_tileir.cc:68-79
m.def("add_fma_fusion", [](mlir::PassManager &pm) {
    auto &mpm = pm.nest<cuda_tile::ModuleOp>();      // 第 1 层：下沉到 cuda_tile.module
    auto &epm = mpm.nest<cuda_tile::EntryOp>();      // 第 2 层：再下沉到 entry
    epm.addPass(cuda_tile::createFuseFMAPass());     // 在 entry 内跑 fuse-fma
});
m.def("add_loop_split", [](mlir::PassManager &pm, int threshold = 1) {
    auto &mpm = pm.nest<cuda_tile::ModuleOp>();
    auto &epm = mpm.nest<cuda_tile::EntryOp>();
    epm.addPass(cuda_tile::createLoopSplitPass({threshold}));  // 在 entry 内跑 loop-split
});
```

对应到 lit 测试里那条 pass-pipeline 字符串，嵌套关系一目了然（`\$` 只是 shell 转义，真实助记符是 `experimental$func`）：

```text
// op-conversion.mlir:1
... --pass-pipeline="builtin.module(
        convert-triton-to-cuda-tile,
        cuda_tile.module(              ← 第 1 层 nest<ModuleOp>
          cuda_tile.experimental$func( ← 第 2 层 nest<EntryOp>
            fuse-fma                   ← createFuseFMAPass()
          )
        ),
        reconcile-unrealized-casts)" ...
```

这段文字 pipeline 与上面 C++ 的两层 `nest` **完全同构**：`cuda_tile.module(...)` 对应 `pm.nest<cuda_tile::ModuleOp>()`，`cuda_tile.experimental$func(...)` 对应 `mpm.nest<cuda_tile::EntryOp>()`。

再看 `add_strip_debuginfo` —— 它只 **嵌套一层**：

```cpp
// triton_tileir.cc:83-87
m.def("add_strip_debuginfo", [](mlir::PassManager &pm) {
    auto &mpm = pm.nest<cuda_tile::ModuleOp>();   // 只到 module 层
    mpm.addPass(mlir::createStripDebugInfoPass()); // 整个 cuda_tile 模块范围内擦除
});
```

为什么 strip 只到 module 层、不继续下钻到 entry？因为剥离调试信息是 **整棵子树通杀** 的操作——挂在 module 层，pass manager 自然会递归遍历它下面的 entry 和所有算子，没必要再指定 entry。

关键证据：这些 pass 在 Python 侧的调用见 [compiler.py:317-319](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L317-L319)：

```python
if opt.enable_fp_fusion:
    tileir.passes.add_fma_fusion(pm)
tileir.passes.add_strip_debuginfo(pm)
```

**请特别注意一个事实**：在 `make_tileir` 整个函数里，**没有任何一行调用 `add_loop_split`**。也就是说 loop-split 虽然在 pybind 里暴露了、在 opt 工具里也注册了（见 4.1.4），但默认编译流水线 **并不启用它**。这一点务必记住，别误以为 loop-split 一定会跑。

> 顺带一提：紧挨着的 `add_synthesize_debug_info_scopes`（triton_tileir.cc:88-92）同样是「已暴露、默认不挂」的状态，本讲不展开。

#### 4.1.4 代码实践

**实践目标**：亲手验证「嵌套深度」与「loop-split 默认未启用」这两个结论。

**操作步骤**（源码阅读型实践，无需 GPU）：

1. 打开 [triton_tileir.cc:68-92](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L68-L92)，数一数 `add_fma_fusion`、`add_loop_split`、`add_strip_debuginfo` 各自调用了几次 `nest<>()`。
2. 打开 [compiler.py:296-330](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L296-L330) 的 `make_tileir`，用搜索确认 `add_loop_split` 在其中 **零出现**。
3. 对照 [op-conversion.mlir:1](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion.mlir#L1) 的 pass-pipeline 文本，把 `cuda_tile.module(...)` / `cuda_tile.experimental$func(...)` 与 C++ 的两层 `nest` 一一对应起来。
4. （可选，需要先按 u4-l1 构建）在 build 目录跑 `triton-cuda-tile-opt`，把 pipeline 里的 `fuse-fma` 换成 `loop-split`，观察 opt 工具确实认识这个 pass——这说明它「能用」，只是 Python 流水线「没用」。

**需要观察的现象**：
- `add_fma_fusion` 与 `add_loop_split` 都是两次 `nest`，`add_strip_debuginfo` 只有一次。
- `make_tileir` 里搜不到 `loop_split`。

**预期结果**：嵌套深度 = `nest` 调用次数；loop-split 属于「工具可用、流水线未接」的预留 pass。如果第 4 步无法本地构建 opt 工具，明确写「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `epm.addPass(cuda_tile::createFuseFMAPass())` 误写成直接挂在最外层 `pm.addPass(...)`（不 nest），会发生什么？

> **参考答案**：pass manager 会在 builtin 模块这一层寻找可处理的算子，而 fuse-fma 关心的 `arith.mulf`/`arith.addf` 都藏在 `cuda_tile.module → entry` 深处，外层根本扫不到，于是 pass 实际上「空跑」，融合不会发生，且不会有任何报错提示——这正是嵌套深度必须写对的原因。

**练习 2**：为什么 `strip-debuginfo` 只 nest 到 `cuda_tile.module`，而 fuse-fma 要再向下 nest 到 entry？

> **参考答案**：strip 是「对整棵子树通杀」的属性级清理，挂在容器层就能递归覆盖所有子节点；fuse-fma 是「按 kernel 逐个优化」的逻辑，必须精确落在每个 entry 的函数体上，且一个 `cuda_tile.module` 可能含多个 entry，下钻到 entry 才能保证每个 kernel 都被处理。

---

### 4.2 bytecode 写出

#### 4.2.1 概念说明

主转换和后处理 pass 全部跑完后，IR 还是内存里的 MLIR 对象。要把它交给外部编译器 `tileiras`（见 u2-l7），必须先 **序列化** 成 `tileiras` 能读的字节流——这就是 `write_bytecode` 的职责。

这里有个关键设计：**`tileiras` 只关心 `cuda_tile` 方言那部分 IR**，对最外层 builtin 模块、以及调试信息之外的任何「外壳」都不感兴趣。所以 `write_bytecode` 不能简单地把整棵 builtin 模块序列化，而要 **先从里面挑出那个 `cuda_tile::ModuleOp` 容器**，只对它做 bytecode 序列化。

#### 4.2.2 核心流程

`write_bytecode` 的三步逻辑：

```
1. 取最外层 builtin ModuleOp 的 body 的【第一个操作】(front)
2. 判断它是不是 cuda_tile::ModuleOp
   - 是   → 拿到它的引用
   - 不是 → 抛异常 "No cuda_tile::ModuleOp found in the input module"
3. 用 cuda_tile::writeBytecode(...) 把这个 cuda_tile 模块序列化为字节串
```

为什么敢直接取 `front()`（第一个操作）？因为主转换 `convert-triton-to-cuda-tile` 的契约就是「把 `cuda_tile::ModuleOp` 插在 builtin 模块体的最前面」（见 u3-l1）。所以这个查找其实是 **依赖主转换的插入约定** 的——约定不变，查找就稳定。

序列化得到的字节串最终在 `make_cubin` 里被写成 `{name}.bytecode` 缓存文件，既供 `tileiras` 消费，也方便事后复现（见 u2-l7）。

#### 4.2.3 源码精读

`write_bytecode` 的全部实现（[triton_tileir.cc:129-149](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L129-L149)）：

```cpp
m.def("write_bytecode", [](mlir::ModuleOp mod) {
    cuda_tile::ModuleOp cudaTileModule;
    if (!mod.getBody()->empty())
      if (auto nestedCudaTileModule =
              dyn_cast<cuda_tile::ModuleOp>(&mod.getBody()->front()))
        cudaTileModule = nestedCudaTileModule;

    if (!cudaTileModule)
      throw std::runtime_error(
          "No cuda_tile::ModuleOp found in the input module");

    std::string buffer;
    llvm::raw_string_ostream ostream(buffer);
    if (failed(cuda_tile::writeBytecode(
            ostream, cudaTileModule,
            cuda_tile::BytecodeVersion::kCurrentVersion)))
      throw std::runtime_error("Failed to write cuda_tile bytecode");
    py::bytes bytes(buffer.data(), buffer.size());
    return bytes;
});
```

逐行解读：

- `mod` 是最外层 **builtin** `ModuleOp`（`mlir::ModuleOp`），不是 cuda_tile 模块。
- `mod.getBody()->front()` 取模块体里的第一个操作——按主转换的约定，它就是 `cuda_tile::ModuleOp`。
- `dyn_cast<cuda_tile::ModuleOp>(...)` 做类型检查：是则赋值，否则 `cudaTileModule` 保持空。
- 空就抛异常，给出明确错误信息。
- `cuda_tile::writeBytecode(...)` 用 `kCurrentVersion` 把 **这个嵌套的 cuda_tile 模块** 序列化到字符串 `buffer`。
- 最后包成 `py::bytes` 返回给 Python。

Python 侧的调用点在 `make_cubin → call_tileiras`（[compiler.py:217-219](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L217-L219)）：

```python
bytecode = tileir.write_bytecode(mod)
bytecode_cache_name = f"{name}.bytecode"
bytecode_file = fn_cache_manager.put(bytecode, bytecode_cache_name)
```

这里有个贯穿全讲的时间线值得强调：`write_bytecode` 序列化的，正是 `make_tileir` 里 fma 融合 + strip debuginfo **全部跑完之后** 的最终 IR。所以「4.1 的后处理」和「4.2 的打包」是连续的两步——先把 IR 打磨干净，再打包成 bytecode。

#### 4.2.4 代码实践

**实践目标**：确认 `write_bytecode` 找的到底是哪个嵌套模块，并理解它为何不能直接序列化最外层模块。

**操作步骤**（源码阅读型实践）：

1. 读 [triton_tileir.cc:129-149](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L129-L149)，回答：函数入参 `mod` 是 builtin ModuleOp 还是 cuda_tile ModuleOp？实际被序列化的是哪个？
2. 追踪调用链：`make_cubin`（[compiler.py:333-334](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L333-L334)）→ `call_tileiras` → `tileir.write_bytecode(mod)`，确认 `mod` 是 `make_tileir` 的返回值。
3. 思考题：如果某个未来的 pass 把 `cuda_tile::ModuleOp` 插到了 builtin 模块体的第二个位置（而不是 `front()`），`write_bytecode` 会怎样？

**需要观察的现象 / 预期结果**：
- 入参 `mod` 是 **builtin** ModuleOp；真正序列化的是它 body 里的 **第一个操作**，且该操作必须是 `cuda_tile::ModuleOp`。
- 第 3 步：`front()` 取到的就不是 cuda_tile 模块了，`dyn_cast` 失败，抛出 `"No cuda_tile::ModuleOp found in the input module"`。这说明 `write_bytecode` 与主转换的「插在最前」约定是强耦合的。

**待本地验证**：若你手头有构建好的环境，可在 Python 里打印 `tileir.write_bytecode` 返回的字节长度，观察它随 `enable_fp_fusion` 开关（影响 fuse-fma 是否挂载）的变化。

#### 4.2.5 小练习与答案

**练习 1**：`write_bytecode` 为什么要用 `dyn_cast` 而不是 `cast`？

> **参考答案**：`dyn_cast` 在类型不匹配时返回空指针，配合下面的 `if (!cudaTileModule)` 能给出友好的 `runtime_error`；若用 `cast`，类型不符会直接触发断言失败，错误信息不友好、且难以在 pybind 层捕获成 Python 异常。

**练习 2**：序列化用的是 `cuda_tile::BytecodeVersion::kCurrentVersion`，这暗示了什么？

> **参考答案**：cuda_tile 的 bytecode 格式有版本概念，写入端用「当前版本」。这意味着 `tileiras` 必须是与该版本匹配的工具链（CUDA 13.1/13.3 的 cuda-tile），版本不匹配可能无法解析——这也是 u2-l7 强调 `CUDA_HOME` 要从 `tileiras` 路径反推、刻意不读系统变量的原因之一。

---

### 4.3 合法性校验收尾

#### 4.3.1 概念说明

`make_tileir` 跑完所有 pass 后，并不是直接把模块交给 `make_cubin`，而是先做一道 **整体验收**：`only_contain_legal_dialects`。它回答一个问题——

> 「整个模块里，还有没有残留的、非 cuda_tile 方言的算子？」

为什么需要这道验收？因为主转换 `convert-triton-to-cuda-tile` 用的是 `applyFullConversion`（见 u3-l1），理论上会把所有 `tt.*`/`scf`/`cf`/`gpu` 算子 lowering 掉。但理论不等于现实——如果某个算子 **没有对应的 lowering 模式**（比如 README 提到的尚未支持的 `tt.gather`、`math.erf` 等），full conversion 会直接报错。然而后续 pass（memtoken、fma 等）有可能引入新的、意料之外的中间状态。`only_contain_legal_dialects` 就是 **最后一道兜底**：把「残留非法算子」的错误前移到编译期、并给出统一清晰的报错，而不是让垃圾 IR 流到 `tileiras` 那里崩出无法理解的错误。

#### 4.3.2 核心流程

校验逻辑非常朴素（一次遍历）：

```
对模块里【每一个】操作 op：
    如果 op 既不是 builtin ModuleOp，
    又不属于 cuda_tile 方言命名空间：
        → 标记「不合法」
返回标记结果
```

调用方根据返回值决定是否抛错：

```
pm.run(mod, "make_tileir")
if not tileir.only_contain_legal_dialects(mod):
    raise RuntimeError("Triton ttir to tileir ir failed. ...")
```

它的位置非常关键——**在 `pm.run()` 之后**，也就是所有 pass（含 fma、strip）都跑完之后才校验。这保证了校验看到的是「最终态」。

> 注意它和 u3-l1 的 `CudaTileConversionTarget` 的区别：后者是 **转换时的合法性目标**（驱动 full conversion 判定哪些算子必须被消除），本讲的 `only_contain_legal_dialects` 是 **转换后的复查**（事后扫描确认确实干净）。两者一前一后，双重保险。

#### 4.3.3 源码精读

C++ 实现（[triton_tileir.cc:117-128](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L117-L128)）：

```cpp
m.def("only_contain_legal_dialects", [](mlir::ModuleOp mod) {
    bool only_contain_legal_dialects = true;
    mod->walk([&](mlir::Operation *op) {
      if (!llvm::isa<mlir::ModuleOp>(op) &&
          (op->getName().getDialectNamespace() !=
              mlir::cuda_tile::CudaTileDialect::getDialectNamespace()
          )) {
        only_contain_legal_dialects = false;
      }
    });
    return only_contain_legal_dialects;
});
```

要点：

- `mod->walk(...)` 是 MLIR 的 **深度优先遍历**，会访问模块树下每一个操作（包括嵌套在 `cuda_tile.module → entry` 深处的算子），所以不会漏检。
- 白名单只有两类：① `mlir::ModuleOp`（builtin 模块本身，以及任何嵌套的 ModuleOp 容器）；② 方言命名空间等于 `cuda_tile` 的操作。
- 任何其它方言（`triton`、`scf`、`cf`、`gpu`、`ub`、`arith`……）的算子都会让结果变 `false`。

> 细节提醒：这里用 `getDialectNamespace()` 字符串比较，意味着只要某 op 的方言名是 `cuda_tile` 就算合法，**不区分具体是哪个 cuda_tile 算子**。也就是说它只校验「方言纯度」，不校验「算子是否被 cuda_tile 方言 verifier 接受」——后者由 MLIR 自带的 verifier 负责。

Python 侧的收尾（[compiler.py:320-330](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L320-L330)）：

```python
pm.run(mod, "make_tileir")
if not tileir.only_contain_legal_dialects(mod):
    raise RuntimeError(
        "Triton ttir to tileir ir failed. Some ttir ops cannot be converted to tileir."
    )

pattern = r"entry @([a-zA-Z0-9_]*)\("
match = re.findall(pattern, mod.__str__())
if len(match) != 1:
    raise RuntimeError("Kernel Name matching fail")
return mod
```

可以看到，合法性校验之后还跟了一个 **kernel 名校验**：用正则 `entry @([a-zA-Z0-9_]*)\(` 在模块文本里找 entry，要求 **恰好 1 个**。这呼应 4.1 讲的「一个 `cuda_tile.module` 可能含多个 entry」——这里强制单 kernel 编译单元，多于一个就报错。

#### 4.3.4 代码实践

**实践目标**：理解什么情况下 `only_contain_legal_dialects` 会返回 `false`，以及它和 full conversion 的关系。

**操作步骤**（源码阅读型实践）：

1. 读 [triton_tileir.cc:117-128](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L117-L128)，列出会被判为「非法」的两类 op（提示：非 ModuleOp 且方言名 ≠ cuda_tile）。
2. 读 [compiler.py:321-324](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L321-L324)，记下失败时抛出的异常类型与文案。
3. 联系 u3-l1 的 `CudaTileConversionTarget`：回想它把哪些方言设为非法，思考「如果 full conversion 本来就会对残留非法算子报错，为什么还要这道复查？」
4. 查阅 README 的 Known issues（如 `tt.gather`、`math.erf` 等不支持算子），推断：若 kernel 用了这些算子，错误最可能在 full conversion 阶段还是在 `only_contain_legal_dialects` 阶段冒出？

**需要观察的现象 / 预期结果**：
- 失败抛 `RuntimeError`，文案含 `"Some ttir ops cannot be converted to tileir"`。
- 第 4 步推断：多数情况下，未支持算子会在 full conversion 阶段（`applyFullConversion`）就因「找不到 lowering 模式」直接报错；`only_contain_legal_dialects` 更多是兜底后续 pass 引入的意外残留。两者都是 **编译期** 错误，不会拖到运行期。

**待本地验证**：第 4 步的推断若要确认，需在本地构造含未支持算子的小 kernel 实际编译，观察报错堆栈落在哪一步。

#### 4.3.5 小练习与答案

**练习 1**：如果一个 `cuda_tile.module` 容器里同时出现了 2 个 `entry`，`only_contain_legal_dialects` 会返回什么？后续会发生什么？

> **参考答案**：`only_contain_legal_dialects` 只看「方言纯度」，`entry` 属于 cuda_tile 方言，所以它会返回 `true`（校验通过）。但紧接着的 kernel 名正则 `re.findall(...)` 会匹配到 2 个 entry，`len(match) != 1` 成立，于是抛 `RuntimeError("Kernel Name matching fail")`。即：多 entry 不是方言问题，而是「单 kernel 编译单元」约束问题。

**练习 2**：为什么 `only_contain_legal_dialects` 用 `walk` 而不是只检查模块的直接子节点？

> **参考答案**：真正的算子都嵌套在 `cuda_tile.module → entry` 深处，直接子节点只有那个 `cuda_tile::ModuleOp` 容器（它当然合法）。必须用 `walk` 深度优先遍历到每一个叶子算子，才能发现藏在深处的非法残留。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个 **源码阅读 + 推理** 任务。

**背景**：同事问你「`fuse-fma` 和 `loop-split` 到底有没有在默认编译里跑？`tileiras` 拿到的 bytecode 是怎么来的？编译失败怎么定位？」请你结合源码给出有依据的回答。

**任务**：

1. **画一张 `make_tileir` 后处理 + 收尾的时间线**，至少包含以下节点，并标出每个节点的「嵌套深度」和「是否默认启用」：
   - 主转换 `convert-triton-to-cuda-tile`
   - `auto-gen-memory-token`
   - `inline`
   - `fuse-fma`
   - `strip-debuginfo`
   - `pm.run`
   - `only_contain_legal_dialects`
   - kernel 名正则
   - （下一阶段）`write_bytecode`
2. **回答三个具体问题**（每题都要引用具体源码行号作为证据）：
   - (a) 为什么 `fuse-fma` 必须嵌套到 `cuda_tile.module → entry`，而 `strip-debuginfo` 只嵌套到 `cuda_tile.module`？
   - (b) `write_bytecode` 在 builtin ModuleOp 里查找的是哪一个嵌套模块？靠什么约定保证一定能找到？
   - (c) 如果一个 kernel 里残留了一个 `tt.load`（triton 方言算子），会在哪一步、以什么异常信息失败？
3. **诚实标注不确定项**：`loop-split` 在默认流水线里到底跑不跑？`fma.mlir` 测试文件目前有没有实际用例？请在回答里明确写出你从源码观察到的事实，而不是想当然。

**预期产出**：一份不超过一页的「问答 + 时间线图」，所有结论都带 `[文件:行号]` 证据；对「loop-split 默认未启用」「fma.mlir 当前为占位文件」这类反直觉事实不回避、不美化。

> 参考证据点：嵌套见 [triton_tileir.cc:68-92](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L68-L92)；流水线挂载见 [compiler.py:296-330](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L296-L330)；bytecode 查找见 [triton_tileir.cc:129-149](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L129-L149)；合法性校验见 [triton_tileir.cc:117-128](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L117-L128)；fma 测试现状见 [fma.mlir:1](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/fma.mlir#L1)（仅 RUN 行）。

## 6. 本讲小结

- 主转换之后的 **cuda_tile 级 pass** 必须按目标算子的所在层级 **嵌套** 挂载：`fuse-fma` 与 `loop-split` 嵌套两层（`cuda_tile.module → entry`），`strip-debuginfo` 只嵌套一层（`cuda_tile.module`）。
- **嵌套深度 = `nest<>()` 的调用次数**，且与 lit 测试里 `cuda_tile.module(cuda_tile.experimental$func(fuse-fma))` 的文字 pipeline 一一对应。
- **`fuse-fma` 受 `enable_fp_fusion` 控制、默认开**，放在 `inline` 之后以最大化融合机会；`strip-debuginfo` 放最后，确保上游 pass 仍能用调试信息。
- **`loop-split` 虽然在 pybind 与 opt 工具里都已暴露，但 `make_tileir` 默认并不挂载它**——这是「能用但没用」的预留 pass，别误判。
- **`write_bytecode` 序列化的不是最外层 builtin 模块**，而是它 body 里的第一个操作——那个 `cuda_tile::ModuleOp` 容器；靠主转换「插在最前」的约定保证可定位，找不到则抛 `"No cuda_tile::ModuleOp found"`。
- **`only_contain_legal_dialects` 是 full conversion 之后的兜底复查**：用 `walk` 深度遍历，只允许 builtin ModuleOp 与 cuda_tile 方言算子，残留非法算子即抛 `RuntimeError`，把错误前移到编译期。

## 7. 下一步学习建议

本讲讲完，**第三单元「MLIR 转换 Pass 体系」就此收尾**——从 u3-l1 的骨架入口到本讲的打包验收，你已经走完了 TTIR → cuda_tile 方言转换的全链路。

接下来建议进入 **第四单元（advanced）**：

- **u4-l1 triton-cuda-tile-opt 工具与 lit/FileCheck 测试**：本讲多次提到「可用 opt 工具复现」，下一讲会手把手教你构建 `triton-cuda-tile-opt`、读懂 pass-pipeline 字符串、用 lit 跑 FileCheck 测试。学完它，你就能把本讲的 `fuse-fma` / `loop-split` 在本地真正跑起来观察 IR 变化。
- **u4-l2 性能调优实践**：`enable_fp_fusion`（本讲 fma 融合的开关）属于数值/性能旋钮之一，下一讲会把 occupancy / num_ctas / num_stages / TMA 偏好等旋钮系统化讲解。
- 若你对「bytecode 交给 tileiras 之后发生了什么、出错如何分类」还想深入，可回看 **u2-l7**；本讲的 `write_bytecode` 正是 u2-l7 的上游。

继续阅读建议：动手把 [op-conversion.mlir](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion.mlir) 的 pass-pipeline 里 `fuse-fma` 换成 `loop-split`（参照本讲 4.1.4 第 4 步），在 u4-l1 学会构建后实际跑一次，亲眼看看两个 pass 对同一份 IR 的不同输出。

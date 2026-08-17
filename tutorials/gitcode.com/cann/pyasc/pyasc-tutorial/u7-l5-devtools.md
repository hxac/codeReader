# 开发者工具：ascir-opt、ascir-translate 与 ascir-lsp

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `bin/` 目录下三个开发者工具各自的用途、入口源码与依赖库。
2. 掌握「用 `ascir-opt` 对一份 `.mlir` 单独跑指定 Pass」的调试工作流，并能把它与 `compiler.py` 的 Pass 调度对应起来。
3. 理解 `ascir-translate -mlir-to-ascendc` 与 Python 侧 `translation.ir_to_ascendc` 调用的是同一份 C++ 发射实现，从而可以用命令行精确复现 Python 导出的 `ascendc.cpp`。
4. 了解 `PYASC_SETUP_DEVTOOLS=1` 如何改变 `setup.py` 的构建目标与打包内容。
5. 看懂 lit 测试的组织方式：`lit.cfg`、`lit.site.cfg.in`、`RUN:` 行与 `FileCheck` 断言如何配合。

## 2. 前置知识

本讲是第 7 单元第 5 讲，默认你已读完 u6-l1（Pass 全景）并了解以下概念，这里只做简要回顾：

- **Pass 与 Pass 流水线**：pyasc 后端用 MLIR Pass 改写 ASC-IR。正常使用时，Pass 由 `python/asc/runtime/compiler.py` 的三个调度函数自动装填执行，用户看不到中间过程。
- **`PYASC_DUMP_PATH`**：设置该环境变量后，编译器会把中间产物落盘，其中 `codegen.mlir` 是跑 Pass 之前的 IR，`ascir.mlir` 是跑完 Pass 之后的 IR，`ascendc.cpp` 是翻译得到的 Ascend C 源码。
- **MlirOptMain / MlirTranslateMain**：MLIR 上游自带的「工具主函数」框架。只要把方言、Pass 注册进一个 `DialectRegistry` 再调用它们，就能免费获得一个命令行工具——支持 `--help`、逐 Pass 打印 IR、`FileCheck` 友好的输出等标准能力。pyasc 的三个工具都建立在这层框架之上，各自只有 20~50 行代码。
- **lit 与 FileCheck**：lit 是 LLVM 系的测试驱动器，扫描测试文件里的 `RUN:` 行当作 shell 命令执行；FileCheck 对命令输出做逐行模式匹配（`CHECK:`、`CHECK-NEXT:`、`CHECK-SAME:` 等前缀）。二者配合实现「输入 IR + 期望输出」的回归测试。

如果你对「为什么需要单独跑某个 Pass」没有感觉，回想 u6-l1 的建议：调试一个 Pass 问题时，在几十个 Pass 的自动流水线里观察某一个的行为非常困难，而把 `codegen.mlir` 交给 `ascir-opt` 单跑目标 Pass，输入输出一目了然。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [bin/ascir-opt.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/ascir-opt.cpp) | ascir-opt 工具入口：注册全部方言与 Pass 后进入 MlirOptMain |
| [bin/ascir-translate.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/ascir-translate.cpp) | ascir-translate 工具入口：注册 `mlir-to-ascendc` 翻译后进入 MlirTranslateMain |
| [bin/ascir-lsp.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/ascir-lsp.cpp) | ascir-lsp 工具入口：注册方言后进入 MlirLspServerMain |
| [bin/CMakeLists.txt](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/CMakeLists.txt) | 三个工具的 CMake 构建定义（目标名、链接库、输出目录） |
| [include/ascir/Dialect/Utils/Registration.h](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Utils/Registration.h) | 公共注册函数：registerDialects / registerExtensions / registerPasses |
| [include/ascir/Target/Asc/Translation.h](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Translation.h) | `translateToAscendC` 声明，命令行工具与 Python 绑定共用 |
| [python/src/Translation.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Translation.cpp) | pybind 绑定：把 `translateToAscendC` 暴露为 `translation.ir_to_ascendc` |
| [python/asc/runtime/compiler.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py) | Python 侧 Pass 调度与四级 dump，是与命令行工作流对照的基准 |
| [setup.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py) | `PYASC_SETUP_DEVTOOLS` 读取、构建目标追加与工具打包 |
| [test/lit.cfg](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/lit.cfg) | lit 主配置：测试名、后缀、执行格式 |
| [test/lit.site.cfg.in](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/lit.site.cfg.in) | lit 站点配置模板：把工具名映射到构建目录 |
| [test/CMakeLists.txt](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/CMakeLists.txt) | `check-ascir` 测试目标：先建工具再跑 lit |
| [test/Dialect/AscendC/Transforms/materialize-tensor.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/materialize-tensor.mlir) | ascir-opt 单跑 Pass 的 lit 用例样例 |
| [test/Target/AscendC/core.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/core.mlir) | ascir-translate 翻译发射的 lit 用例样例 |
| [test/build_llt.sh](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh) | C++ lit 测试的脚本入口（`make check-ascir` / 精确模式） |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**ascir-opt**、**ascir-translate**、**ascir-lsp**、**PYASC_SETUP_DEVTOOLS**。前三个是「工具本身」，最后一个是「工具如何被构建与安装」。

### 4.1 ascir-opt：单跑 Pass 的 IR 优化器

#### 4.1.1 概念说明

`ascir-opt` 是 pyasc 版的 `mlir-opt`：读入一份 `.mlir` 文本，按命令行指定的 Pass（或 Pass 流水线）改写后打印到标准输出。它存在的意义是**把 Pass 从 Python 自动流水线里拆出来单独观察**：

- 怀疑某个 Pass 改错了 IR？拿 `codegen.mlir` 只跑它一个，输入输出直接对比。
- 想知道 Pass A 的输出是不是 Pass B 的合法输入？把两个 Pass 串在一条命令里。
- 想给新写的 Pass 写回归测试？照抄 `test/Dialect/AscendC/Transforms/` 下任意一个 `.mlir` 的 `RUN:` + `CHECK:` 模式。

#### 4.1.2 核心流程

```
读取 .mlir 文本
    │
    ▼
按 -ascendc-xxx 选项构造 Pass 流水线
    │
    ▼
逐 Pass 改写模块（可加 --mlir-print-ir-after-all 观察每步）
    │
    ▼
打印改写后的 .mlir 到 stdout
```

Pass 的命令行名就是 [Passes.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td) 里 `Pass<"ascendc-materialize-tensor", ...>` 的第一个字符串加一个连字符，例如：

| Pass 类名 | 命令行选项 | 作用（回顾 u6-l1/u6-l2） |
| --- | --- | --- |
| MaterializeTensor | `-ascendc-materialize-tensor` | 把惰性张量物化为队列分配 |
| UnifyPipe | `-ascendc-unify-pipe` | 合并多个 pipe 为一个 |
| InsertSync | `-ascendc-insert-sync` | 自动插入队列同步 |
| VerifySync | `-ascendc-verify-sync` | 校验 alloc/enque/deque/free 配对 |
| Noop | `-ascendc-noop` | 空 Pass，最小模板 |

#### 4.1.3 源码精读

工具入口只有 26 行（含版权头）。真正的逻辑全在「注册 + 交给框架」两步：

```cpp
int main(int argc, char** argv)
{
    DialectRegistry registry;
    ascir::registerDialects(registry);
    ascendc::registerInlinerInterfaces(registry);
    ascir::registerExtensions(registry);
    ascir::registerPasses();
    return asMainReturnCode(MlirOptMain(argc, argv, "AscIR modular optimizer driver\n", registry));
}
```

[bin/ascir-opt.cpp:L18-L26](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/ascir-opt.cpp#L18-L26) 中，main 依次注册方言、内联接口、扩展与 Pass，最后把 `argc/argv` 整体交给 MLIR 的 `MlirOptMain`。命令行解析、`.mlir` 解析、Pass 流水线执行、结果打印全部由框架完成——这正是 u5-l1 说过的「TableGen/框架驱动开发」思想在工具层的体现。

注册函数的实现在公共头里：

```cpp
inline void registerDialects(DialectRegistry& registry)
{
    registerAllDialects(registry);
    registry.insert<ascendc::AscendCDialect, emitasc::EmitAscDialect>();
    ascendc::registerExternalModels(registry);
    emitasc::registerExternalModels(registry);
}
...
inline void registerPasses()
{
    registerAllPasses();
    registerascendcPasses();
}
```

[include/ascir/Dialect/Utils/Registration.h:L25-L38](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Utils/Registration.h#L25-L38)：`registerDialects` 先注册 MLIR **全部上游方言**（scf、arith、func……），再额外插入 pyasc 自己的 `ascendc` 与 `emitasc` 两个方言及其外部模型；`registerPasses` 同样先注册上游全部 Pass，再追加 `registerascendcPasses()`（即 u6-l1 讲过的 16 个自定义 Pass 的注册入口）。所以 `ascir-opt` 能同时理解通用方言操作和 `ascendc.*` 操作。

lit 用例是学习这个工具的最佳文档。看一个真实的单 Pass 测试：

```
// RUN: ascir-opt -ascendc-materialize-tensor %s | FileCheck %s

// CHECK-LABEL: func.func @materialize_tensor_static
// CHECK:      %0 = ascendc.pipe
// CHECK-NEXT: %1 = ascendc.queue : <vecin, 1>
// CHECK-NEXT: ascendc.pipe.init_queue %0, %1, %c1_i32, %c256_i64 : ...
// CHECK-NEXT: %2 = ascendc.que_bind.alloc_tensor %1 : ...
```

[test/Dialect/AscendC/Transforms/materialize-tensor.mlir:L9-L14](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/materialize-tensor.mlir#L9-L14) 的 `RUN:` 行就是一条完整的命令行用法：`ascir-opt -ascendc-materialize-tensor %s | FileCheck %s`——`%s` 代表测试文件自身，输出交给 FileCheck 用 `CHECK` 系列断言逐行验证。这也正是 u6-l2 精读 MaterializeTensor 时引用的行为快照的来源。

#### 4.1.4 代码实践

**实践目标**：用 `ascir-opt` 对 Python 侧 dump 出的 `codegen.mlir` 单独跑两个 lowering Pass，观察 IR 变化。

**操作步骤**：

1. 确认手头有 `ascir-opt` 可执行文件（构建方式见 4.4；下文假设其在 `<构建目录>/bin/ascir-opt`）。
2. 准备输入 IR：设置 `PYASC_DUMP_PATH=/tmp/add_dump` 后运行 `examples/01_add/add.py`（Model 模式即可），得到 `/tmp/add_dump/codegen.mlir`。01_add 使用惰性张量风格，正好是 lowering Pass 的作用对象。
3. 单跑第一个 Pass：
   ```bash
   <构建目录>/bin/ascir-opt -ascendc-materialize-tensor /tmp/add_dump/codegen.mlir > /tmp/after_mat.mlir
   ```
4. 串上第二个 Pass（在上一条命令的输出上继续）：
   ```bash
   <构建目录>/bin/ascir-opt -ascendc-unify-pipe /tmp/after_mat.mlir > /tmp/after_unify.mlir
   ```
   也可以写成一条命令：`ascir-opt -ascendc-materialize-tensor -ascendc-unify-pipe /tmp/add_dump/codegen.mlir`。
5. 想逐步观察时加打印选项重跑：`ascir-opt --mlir-print-ir-after-all -ascendc-materialize-tensor ...`。

**需要观察的现象**：

- `after_mat.mlir` 相比 `codegen.mlir`：`ascendc.local_tensor_auto` 消失，取而代之的是 `ascendc.pipe`、`ascendc.queue`、`ascendc.pipe.init_queue` 与 `ascendc.que_bind.alloc_tensor`（与上面 lit 用例的 CHECK 行一致）。
- `after_unify.mlir` 相比 `after_mat.mlir`：若第一步产生了多个 `ascendc.pipe`，它们被合并为一个。

**预期结果**：两个 Pass 的效果与 [compiler.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L120-L131) 中 `_schedule_lowering` 按相同顺序调用 `add_materialize_tensor`、`add_unify_pipe` 得到的结果一致——命令行只是复现了 Python 流水线的片段。具体 diff 内容**待本地验证**（取决于你机器上实际 dump 的 IR）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ascir-opt` 能解析 `scf.for`、`arith.constant` 这些非 pyasc 方言的操作？

**答案**：因为 [Registration.h](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Utils/Registration.h#L25-L31) 的 `registerDialects` 先调用了 `registerAllDialects(registry)` 注册 MLIR 全部上游方言，再插入 `ascendc`/`emitasc`。dump 出的 IR 混合了通用方言与自定义方言，两者都必须可解析。

**练习 2**：`-ascendc-verify-sync` 这个选项名从哪里来？如何查到全部可用 Pass 选项？

**答案**：选项名来自 [Passes.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L95) 中 `def VerifySync : Pass<"ascendc-verify-sync", "func::FuncOp">` 的第一个参数。运行 `ascir-opt --help` 可以列出全部已注册选项（待本地验证具体输出）。

**练习 3**：如果想让某个 Pass 只作用于模块里的一个函数，应该依赖什么机制？

**答案**：pyasc 在 Python 侧用嵌套 Pass（`addNestedPass` 挂到 FuncOp，见 u6-l1）；命令行侧单跑时 Pass 按其声明的作用域（`Pass<..., "func::FuncOp">`）自动作用于每个嵌套函数，无需额外参数。

### 4.2 ascir-translate：IR 与 Ascend C 互转

#### 4.2.1 概念说明

`ascir-translate` 是 pyasc 版的 `mlir-translate`：把一份 ASC-IR 文本翻译成 Ascend C 源码打印出来。它与 Python 流程的关系是本讲最关键的一句话：

> **`ascir-translate -mlir-to-ascendc` 与 Python 侧 `translation.ir_to_ascendc()` 最终调用的是同一个 C++ 函数 `translateToAscendC`。**

也就是说，u6-l5 精读的整套发射层（Translation 的 TypeSwitch 分发、CodeEmitter、EmitNameStack）对两条入口完全共享；命令行入口只是绕过了 Python 的 JIT、缓存与毕昇编译，直接做「IR 文本 → C 文本」这一步。

#### 4.2.2 核心流程

```
ascir-translate -mlir-to-ascendc input.mlir
    │
    ▼
解析 .mlir → Operation
    │
    ▼
translateToAscendC(op, ostream)     ← 与 Python 共用的发射实现
    │
    ▼
Ascend C 源码打印到 stdout
```

#### 4.2.3 源码精读

命令行入口注册了一个名为 `mlir-to-ascendc` 的翻译：

```cpp
int main(int argc, char** argv)
{
    registerAllTranslations();

    TranslateFromMLIRRegistration reg(
        "mlir-to-ascendc", "translate from mlir to Ascend C",
        [](Operation* op, raw_ostream& output) { return translateToAscendC(op, output); },
        [](DialectRegistry& registry) {
            registry.insert<...>();
            ascendc::registerExternalModels(registry);
            emitasc::registerExternalModels(registry);
        });

    return failed(mlirTranslateMain(argc, argv, "AscIR translation tool"));
}
```

[bin/ascir-translate.cpp:L31-L51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/ascir-translate.cpp#L31-L51)：`TranslateFromMLIRRegistration` 的第三个参数是翻译回调，直接转发给 `translateToAscendC`；第四个参数列出该翻译涉及的方言（arith、ascendc、cf、DLTI、emitasc、emitc、func、LLVM、math、memref、scf）。`-mlir-to-ascendc` 就是这条注册产生的命令行选项名。

`translateToAscendC` 的声明只有一行：

```cpp
LogicalResult translateToAscendC(Operation* op, raw_ostream& os);
```

[include/ascir/Target/Asc/Translation.h:L17-L21](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Translation.h#L17-L21)。实现位于 `lib/Target/AscendC/Translation.cpp`（u6-l5 已精读），返回 `LogicalResult` 表示发射是否完整覆盖了所有 Op。

Python 侧的绑定则把它包成返回字符串的函数：

```cpp
m.def("ir_to_ascendc", [](ModuleOp& mod) -> std::string {
    std::string result;
    llvm::raw_string_ostream os(result);
    if (translateToAscendC(mod.getOperation(), os).failed())
        throw std::runtime_error("Failed to translate IR to Ascend C");
    os.flush();
    return result;
});
```

[python/src/Translation.cpp:L30-L37](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Translation.cpp#L30-L37)：`translation.ir_to_ascendc` 把输出写到字符串再返回给 Python，失败时抛 `runtime_error`。

Python 编译器在跑完 Pass 之后调用它并落盘：

```python
@staticmethod
def run_translation(mod: ir.ModuleOp) -> str:
    return translation.ir_to_ascendc(mod)
```

[python/asc/runtime/compiler.py:L116-L117](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L116-L117)，以及调用点 [compiler.py:L162-L173](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L162-L173)：`run` 先 dump `codegen.mlir`，再 `run_passes`，再 dump `ascir.mlir`，然后 `run_translation` 得到源码并 dump 为 `ascendc.cpp`。把这三行与命令行对照，就得到本讲的复现公式：

```
ascendc.cpp 的内容 == ascir-translate -mlir-to-ascendc ascir.mlir 的输出
```

lit 用例同样以命令行为准：

```
// RUN: ascir-translate -mlir-to-ascendc %s | FileCheck %s

// CHECK-LABEL:void emit_local_tensor(AscendC::LocalTensor<float> v1, uint32_t v2, ...) {
// CHECK-NEXT:   AscendC::LocalTensor<float> v5 = v1[v4];
```

[test/Target/AscendC/core.mlir:L9-L13](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/core.mlir#L9-L13)：输入 IR 里的每个 `ascendc` 操作被发射成对应的 `AscendC::LocalTensor` 成员调用——u6-l5 讲过的「一个 Op → 一条 Ascend C 调用」在测试里逐行可见。

#### 4.2.4 代码实践

**实践目标**：验证「命令行翻译 == Python 导出的 ascendc.cpp」。

**操作步骤**：

1. 沿用 4.1.4 产生的 `/tmp/add_dump/`，其中已有 `ascir.mlir`（Pass 后 IR）和 `ascendc.cpp`（Python 翻译结果）。
2. 命令行翻译：
   ```bash
   <构建目录>/bin/ascir-translate -mlir-to-ascendc /tmp/add_dump/ascir.mlir > /tmp/ascendc_cli.cpp
   ```
3. 对比：
   ```bash
   diff /tmp/ascendc_cli.cpp /tmp/add_dump/ascendc.cpp
   ```

**需要观察的现象**：

- 正常情况下 diff 为空（发射器是确定性的，且 IR 相同）。
- 若你在运行示例时启用了 debug 选项（`enable_debug`），Python 侧会在 `ascendc.cpp` 里额外注入 InitDump 代码块（[compiler.py:L169-L170](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L169-L170) 的 `_gen_init_dump_code`），此时 diff 会显示这段多出来的内容——这正好证明「注入发生在翻译之后」。

**预期结果**：两份文件逐字节一致（无 debug 注入时）。**待本地验证**：如果 diff 不为空，先检查是否发生了缓存命中导致 `ascir.mlir` 与 `ascendc.cpp` 不是同一次编译的产物（dump 仅在真编译时刷新，见 u1-l5），可加 `always_compile=True` 重跑后再对比。

#### 4.2.5 小练习与答案

**练习 1**：为什么对比时要拿 `ascir.mlir` 而不是 `codegen.mlir` 去翻译？

**答案**：`codegen.mlir` 是 Pass 之前的 IR，其中的惰性张量等形态未经过 lowering，直接翻译会因存在未登记的 Op 而失败（发射层白名单机制，见 u6-l5）；`ascir.mlir` 才是与 `ascendc.cpp` 同一输入。若坚持用 `codegen.mlir`，需要先用 `ascir-opt` 把 lowering/收尾 Pass 补齐。

**练习 2**：`ascir-translate` 里 `registerAllTranslations()` 注册了什么？会不会与 `-mlir-to-ascendc` 冲突？

**答案**：它注册 MLIR 上游自带的一批翻译（如 mlir-to-llvmir 等），使工具具备通用翻译能力；`-mlir-to-ascendc` 是在其之上追加的 pyasc 专属注册，名字不同不会冲突。具体可用 `ascir-translate --help` 查看（待本地验证）。

### 4.3 ascir-lsp：面向编辑器的语言服务

#### 4.3.1 概念说明

`ascir-lsp` 是 pyasc 版的 `mlir-lsp-server`：一个遵循 **LSP（Language Server Protocol，语言服务器协议）** 的后台进程，通过标准输入输出与编辑器（VS Code、Neovim 等任何支持 LSP 的客户端）用 JSON-RPC 消息通信。它让编辑器「看懂」`.mlir` 文件，提供语法检查、悬停提示、跳转定义等能力。对经常手写或修改 IR 的后端开发者（比如调试 lit 用例时）最有价值。

三个工具中它最「薄」：不注册 Pass、不做翻译，只需要方言能被解析。

#### 4.3.2 核心流程

```
编辑器（LSP 客户端） ⇄ JSON-RPC over stdio ⇄ ascir-lsp 进程
                                              │
                                              ▼
                                   用注册的方言解析 .mlir，
                                   按协议返回诊断/补全/悬停
```

#### 4.3.3 源码精读

```cpp
int main(int argc, char** argv)
{
    DialectRegistry registry;
    ascir::registerDialects(registry);
    ascir::registerExtensions(registry);
    return MlirLspServerMain(argc, argv, registry).failed();
}
```

[bin/ascir-lsp.cpp:L18-L24](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/ascir-lsp.cpp#L18-L24)：与 `ascir-opt` 相比少了两步——`registerInlinerInterfaces` 与 `registerPasses`，因为语言服务不需要跑 Pass 流水线；多出来的 `MlirLspServerMain` 负责整个 LSP 事件循环。

CMake 侧，三个工具都链接全部方言库，差异在各自的主框架库：

- [bin/CMakeLists.txt:L15-L29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/CMakeLists.txt#L15-L29)：`ascir-lsp` 额外链接 `MLIRLspServerLib`；
- [bin/CMakeLists.txt:L64-L77](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/CMakeLists.txt#L64-L77)：`ascir-opt` 链接 `MLIROptLib`/`MLIRMlirOptMain` 与 `MLIRAscTransforms`；
- [bin/CMakeLists.txt:L90-L108](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/CMakeLists.txt#L90-L108)：`ascir-translate` 链接 `MLIRTranslateLib` 与发射层库 `MLIRTargetAsc`。

三者输出目录统一设为 `${CMAKE_BINARY_DIR}/bin`，所以构建后都能在构建目录的 `bin/` 下找到。

#### 4.3.4 代码实践

**实践目标**：确认 `ascir-lsp` 是一个可用的 LSP 服务器。

**操作步骤**：

1. 运行 `<构建目录>/bin/ascir-lsp --help` 查看命令行选项。
2. 若使用支持 LSP 的编辑器，把 `ascendc`/`emitasc` 相关文件类型（`.mlir`）的语言服务器配置指向该可执行文件（例如 Neovim 的 `vim.lsp.start` 或 VS Code 的通用 LSP 插件），打开 4.1.4 中的 `/tmp/add_dump/codegen.mlir`。

**需要观察的现象**：编辑器状态栏显示语言服务器已连接；故意把某个 `ascendc.` 操作名改错，应出现语法/解析诊断。

**预期结果**：能对非法 IR 给出诊断提示。具体连接方式随编辑器而异，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ascir-lsp` 不需要 `registerPasses()`？

**答案**：LSP 的核心工作是解析与静态分析（诊断、悬停等），不执行 Pass 改写；`ascir-opt` 需要 Pass 注册才能构造流水线，语言服务器不需要。

**练习 2**：三个工具中哪一个链接了发射层 `MLIRTargetAsc`？为什么？

**答案**：只有 `ascir-translate`（见 [bin/CMakeLists.txt:L103](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/CMakeLists.txt#L103)）。因为只有它需要把 IR 翻译成 Ascend C，而发射实现位于 `lib/Target/AscendC`，对应库即 `MLIRTargetAsc`。

### 4.4 PYASC_SETUP_DEVTOOLS：工具的构建与安装

#### 4.4.1 概念说明

回顾 u1-l2：`pip install .` 默认只构建一个目标——pybind 扩展模块 `libpyasc`。三个命令行工具是**可选构建目标**，由环境变量 `PYASC_SETUP_DEVTOOLS` 控制。这体现了安装体积的取舍：普通用户只需要 `libpyasc`，后端开发者才需要工具链。

#### 4.4.2 核心流程

```
PYASC_SETUP_DEVTOOLS=1（/true/ON）
    │
    ▼
get_requested_devtools() 返回 ("ascir-lsp","ascir-opt","ascir-translate")
    │
    ├── build_ext 阶段：cmake --build --target libpyasc ascir-lsp ascir-opt ascir-translate
    │       产物落在 <构建目录>/cmake.<平台后缀>/bin/
    └── 打包阶段：data_files 把三个可执行文件装进环境的 bin/ 目录
```

#### 4.4.3 源码精读

开关的读取：

```python
@functools.lru_cache(maxsize=1)
def get_requested_devtools() -> Tuple[str, ...]:
    if check_env_bool("PYASC_SETUP_DEVTOOLS"):
        return "ascir-lsp", "ascir-opt", "ascir-translate"
    return tuple()
```

[setup.py:L174-L178](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L174-L178)。`check_env_bool`（[setup.py:L41-L42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L41-L42)）只认 `"1"`、`"true"`、`"ON"` 三个值。

构建目标的追加：

```python
targets = ["libpyasc", *get_requested_devtools()]
...
build_args = [cmake, "--build", cmake_dir, "--target", *targets, "--parallel"]
```

[setup.py:L255-L266](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L255-L266)：不开开关时 `targets` 只有 `libpyasc`，CMake 根本不会编译三个工具的源文件。

安装进 Python 环境：

```python
devtools = get_requested_devtools()
if devtools:
    print("packaging development tools:", *devtools)
    data_files = [("bin", [str(get_cmake_dir() / "bin" / tool) for tool in devtools])]
```

[setup.py:L379-L382](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L379-L382)：`data_files` 的 `"bin"` 目的地使工具随 `pip install` 落到环境前缀的 `bin/` 下（虚拟环境里就是 `venv/bin/ascir-opt`），之后可直接裸名调用。

lit 侧如何找到这些工具？主配置只定义格式：

```python
config.name = "AscIR"
config.suffixes = [".mlir"]
config.test_format = lit.formats.ShTest()
config.test_source_root = os.path.dirname(__file__)
```

[test/lit.cfg:L1-L6](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/lit.cfg#L1-L6)——名为 AscIR 的测试套件、扫描 `.mlir` 后缀、每条 `RUN:` 按 shell 执行。工具的**路径解析**在站点配置模板里：

```python
config.ascir_src_root = path(r"@ASCIR_SOURCE_DIR@")
config.ascir_tools_dir = path(r"@ASCIR_BINARY_DIR@")
config.llvm_tools_dir = path(r"@LLVM_PREFIX_PATH@/bin")
...
lit.llvm.llvm_config.add_tool_substitutions(["ascir-opt", "ascir-translate"], [config.ascir_tools_dir])
lit.llvm.llvm_config.add_tool_substitutions(["FileCheck"], [config.llvm_tools_dir])
```

[test/lit.site.cfg.in:L1-L11](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/lit.site.cfg.in#L1-L11)：`@...@` 占位符由 CMake 的 `configure_lit_site_cfg` 填充（见 [test/CMakeLists.txt:L11-L16](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/CMakeLists.txt#L11-L16)）；`add_tool_substitutions` 把 `RUN:` 行里裸写的 `ascir-opt`、`ascir-translate` 替换为构建目录下的绝对路径，`FileCheck` 则来自 LLVM 前缀。这就是 `RUN:` 行可以只写工具名的原因。

`check-ascir` 目标把「先建工具、再跑测试」串起来（[test/CMakeLists.txt:L18-L23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/CMakeLists.txt#L18-L23)，依赖表含 `ascir-opt`、`ascir-translate`，注意不含 `ascir-lsp`）。此外还有一个「方言冒烟测试」值得一看：

```
// RUN: ascir-opt -show-dialects %s | FileCheck %s

module {}

// CHECK: Available Dialects
// CHECK-SAME: ascendc
// CHECK-SAME: emitasc
```

[test/tools/ascendir-opt/dialects.mlir:L11-L17](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/tools/ascendir-opt/dialects.mlir#L11-L17)：空模块配合 `-show-dialects`，只断言两个自定义方言已注册——这是检验「工具注册链没被改坏」的最小用例。旁边的 [test/tools/pyasc/lit.local.cfg:L1](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/tools/pyasc/lit.local.cfg#L1) 用 `config.suffixes = [".py"]` 为该子树改写了扫描后缀，说明 lit 支持按目录局部调整配置。

#### 4.4.4 代码实践

**实践目标**：亲手构建出三个工具并确认安装位置。

**操作步骤**：

1. 在源码根目录执行（沿用 u1-l2 的 LLVM 环境变量）：
   ```bash
   PYASC_SETUP_DEVTOOLS=1 python3 -m pip install -e .
   ```
2. 构建完成后查看两个位置：
   ```bash
   ls build/cmake.*/bin/          # CMake 产物目录
   ls $(python3 -c 'import sys; print(sys.prefix)')/bin/ | grep ascir   # 安装目的地
   ```
3. 不设开关重新执行 `pip install -e .`，再对比 `build/cmake.*/bin/` 是否不再更新工具（可先删除再构建验证）。

**需要观察的现象**：安装日志出现 `packaging development tools: ascir-lsp ascir-opt ascir-translate`（来自 [setup.py:L381](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L381) 的 print）；`ascir-opt --help` 可执行。

**预期结果**：三个可执行文件同时出现在构建目录 `bin/` 与环境 `bin/` 下。注意完整构建需要 LLVM 预编译包与 cmake/ninja，本实践**待本地验证**（在无 NPU 的 Linux 机器上即可完成，编译期不依赖昇腾硬件）。

#### 4.4.5 小练习与答案

**练习 1**：`PYASC_SETUP_DEVTOOLS=yes` 能打开开关吗？

**答案**：不能。`check_env_bool` 只接受 `"1"`、`"true"`、`"ON"` 三种值（[setup.py:L41-L42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/setup.py#L41-L42)），其他值一律视为关闭。

**练习 2**：为什么 `check-ascir` 的依赖里有 `llvm-config`？

**答案**：lit 站点配置经由 `lit.llvm.initialize` 走 LLVM 的 lit 基础设施，`FileCheck` 也从 `@LLVM_PREFIX_PATH@/bin` 解析（[test/lit.site.cfg.in:L4-L11](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/lit.site.cfg.in#L4-L11)），因此测试目标声明了对 llvm-config 的依赖以保证这套基础设施可用。

**练习 3**：`build_llt.sh` 的精确测试模式（`CPP_TEST_TARGET=模块名`）如何复用本讲的工具？

**答案**：[test/build_llt.sh:L372-L386](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L372-L386) 中，精确模式先 `make ascir-opt ascir-translate` 只构建两个工具，再对 `test/Target/AscendC/<模块>.mlir`（或同名目录）运行 lit，并用 `--param ascir_tools_dir=${BUILD_DIR}/bin` 告诉 lit 工具在哪——这正是 4.4.3 讲的路径解析机制的另一种喂参方式。

## 5. 综合实践

把四个模块串成一条完整的命令行调试流水线，**不写一行 Python 调用**，复现 Python 编译器的前半程：

1. **准备**（依赖 u1-l4、u1-l5）：`PYASC_SETUP_DEVTOOLS=1` 构建出工具；`PYASC_DUMP_PATH=/tmp/dump` 运行 `examples/01_add/add.py` 得到 `codegen.mlir`、`ascir.mlir`、`ascendc.cpp`。
2. **手动 lowering**：用 `ascir-opt` 依次执行 `-ascendc-input-output-tensor`、`-ascendc-hoist-ub-allocation`、`-ascendc-materialize-tensor`、`-ascendc-unify-pipe`（顺序照抄 [compiler.py:L120-L131](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L120-L131) 的 `_schedule_lowering`），加 `--mlir-print-ir-after-all` 观察每步。
3. **对答案**：把手动 lowering 的最终结果与 `ascir.mlir` 做文本对比（允许上游 canonicalizer/cse 造成的等价差异，记录差异点并判断是否等价）。
4. **翻译验证**：`ascir-translate -mlir-to-ascendc ascir.mlir | diff - ascendc.cpp`，应为空 diff（无 debug 注入时）。
5. **写一个回归用例**：仿照 [materialize-tensor.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/materialize-tensor.mlir#L9-L14) 的格式，从你的 `codegen.mlir` 里截取一段最小 IR，配上 `RUN:` 与三条 `CHECK:`，放进临时目录用 `lit --param ascir_tools_dir=<构建目录>/bin` 跑通。

完成标志：你能回答「Python 编译器在 `_schedule_lowering` 之后、`run_translation` 之前，IR 长什么样」——而且这个答案是你用命令行亲手导出的。步骤 2 的逐步输出与步骤 3 的等价性判断**待本地验证**。

## 6. 本讲小结

- `bin/` 下三个工具都是「20~50 行入口 + MLIR 框架主函数」的结构：`ascir-opt` 用 MlirOptMain 跑 Pass、`ascir-translate` 用 MlirTranslateMain 做翻译、`ascir-lsp` 用 MlirLspServerMain 提供语言服务；差异全在「注册了什么、链接了哪个框架库」。
- 公共注册逻辑沉淀在 `Registration.h`：`registerDialects` 先收全部上游方言再插入 `ascendc`/`emitasc`，`registerPasses` 追加 16 个自定义 Pass——这让每个工具都能完整理解 dump 出的混合方言 IR。
- `ascir-translate -mlir-to-ascendc` 与 Python 的 `translation.ir_to_ascendc` 共用同一个 `translateToAscendC`，因此「翻译 `ascir.mlir`」与「Python 导出的 `ascendc.cpp`」应当逐字节一致，这是验证环境与复现问题的可靠手段。
- Pass 的命令行名就是 `Passes.td` 里 `Pass<"ascendc-xxx">` 的注册名加连字符；单跑、串联、`--mlir-print-ir-after-all` 逐观察构成 Pass 调试三板斧。
- `PYASC_SETUP_DEVTOOLS=1`（仅接受 1/true/ON）让 `setup.py` 把三个工具追加进 CMake 构建目标并作为 `data_files` 装进环境 `bin/`；默认安装只构建 `libpyasc`。
- lit 测试的组织：`lit.cfg` 定格式与 `.mlir` 后缀，`lit.site.cfg.in` 由 CMake 填充并把裸工具名替换为构建目录绝对路径，`RUN:` + `FileCheck` 构成断言；`check-ascir` 目标与 `build_llt.sh` 精确模式都会先构建 `ascir-opt`/`ascir-translate` 再驱动 lit。

## 7. 下一步学习建议

- 下一讲 **u7-l6 测试体系与贡献流程** 会把本讲的 lit 用例扩展成完整的三层测试地图（python/test 的 pytest、kernels/generalization 端到端、test 的 lit），并串讲 `build_llt.sh` 全量用法与本讲 `data_files`、`check-ascir` 在 CI 中的位置——建议接着读。
- 想加深 Pass 调试能力：回到 u6-l1 的 Pass 全景表，挑一个本讲没跑过的 Pass（如 `-ascendc-insert-sync`），在 `test/Dialect/AscendC/Transforms/` 找它的 lit 用例，先读懂 CHECK 断言，再用命令行在 `codegen.mlir` 上复现。
- 想了解工具主函数的更多免费能力（Pass 统计、Pass 时间线、split-input-file 等测试友好选项），可阅读 MLIR 上游 `mlir-opt` 文档并与 `ascir-opt --help` 对照。
- 若你准备给 pyasc 贡献代码：为一个新 Op 补一个 `test/Target/AscendC/` 下的发射用例、或为新 Pass 补一个 `Transforms/` 用例，是本讲技能最直接的落地场景（具体流程见 u7-l6 与 `docs/developer_guide.md`）。

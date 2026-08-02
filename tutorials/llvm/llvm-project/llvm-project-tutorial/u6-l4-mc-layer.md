# MC 层：机器代码与目标文件

## 1. 本讲目标

学完本讲，你应当能够：

- 说清后端流水线的「最后一公里」——MC 层——把机器指令发射成目标汇编（`.s`）或目标文件（`.o`）的整体流程；
- 掌握 `MCContext`（上下文/符号表/段唯一化）、`MCStreamer`（流式发射器及其「汇编流 vs 对象流」两条分支）这两个核心抽象的职责与协作；
- 理解 `TargetRegistry` 如何用一张全局链表统一注册所有目标后端，以及 `llvm-mc`、`llc`、`clang` 等工具如何凭 triple「按名/按架构」查到正确的 `Target`；
- 能用 `llvm-mc`、`llvm-objdump` 等工具亲手走一遍「汇编 → 目标文件 → 反汇编」的往返，并对照源码解释每一步。

## 2. 前置知识

本讲承接 u6-l1（后端流水线总览）。那里已经建立两个关键认知，本讲会直接使用：

- **后端 IR 即 MachineIR（MIR）**：层次是 `MachineFunction ⊃ MachineBasicBlock ⊃ MachineInstr`，与前端 IR 结构相似但操作的是寄存器与机器操作数，因此不复用前端类。
- **代码发射是后端的终点**：寄存器分配、晚期优化之后，后端必须把 `MachineInstr` 变成真正可以交给链接器/汇编器的产物。

本讲需要补充的几个术语：

- **MC（Machine Code）层**：LLVM 中专门负责「机器指令表示 + 汇编/反汇编 + 目标文件格式」的一层，代码集中在 `llvm/lib/MC` 与 `llvm/include/llvm/MC`。它的核心数据是 `MCInst`（一条目标机器指令）。
- **目标文件格式（object file format）**：ELF（Linux 等）、COFF（Windows）、Mach-O（macOS/iOS）、Wasm、XCOFF、GOFF、SPIRV、DXContainer 等。`MCContext` 在构造时就依据 triple 选定一种「环境」。
- **triple**：形如 `x86_64-unknown-linux-gnu` 的目标描述串，编码了架构、厂商、操作系统与对象格式（u1-l3 已介绍）。MC 层大量依赖它来挑选正确实现。
- **fixup / relaxation（修正/松弛）**：一条指令若引用了当时还无法确定最终地址的符号（如分支目标），就先记录一个「修正点」；若指令长度本身可能变化（如变长分支），则需「松弛」迭代到稳定。本讲会在 `emitInstToData` 处点到为止。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `llvm/lib/MC/MCContext.cpp` | `MCContext` 的实现：构造时按 triple 选对象格式环境、管理符号表与各格式段的唯一化表。 |
| `llvm/include/llvm/MC/MCContext.h` | `MCContext` 的类定义：持有 `MCAsmInfo`/`MCRegisterInfo`/`MCObjectFileInfo` 等所有 MC 子系统、各格式 uniquing map、`getOrCreateSymbol` 等接口。 |
| `llvm/lib/MC/MCStreamer.cpp` | `MCStreamer` 基类与 `MCTargetStreamer`（目标特定指令处理）的实现。 |
| `llvm/include/llvm/MC/MCStreamer.h` | `MCStreamer` 抽象基类定义，给出 `emitInstruction`/`switchSection`/`emitLabel`/`finish` 等流式发射接口。 |
| `llvm/lib/MC/MCObjectStreamer.cpp` | `MCObjectStreamer`（对象流）实现：把 `MCInst` 编码成字节并附加 fixup、`finishImpl` 触发布局与落盘。 |
| `llvm/include/llvm/MC/MCObjectStreamer.h` | `MCObjectStreamer` 类定义，是 `MCELFStreamer`/`MCWinCOFFStreamer`/`MCWasmStreamer` 等各格式流的共同中段。 |
| `llvm/lib/MC/TargetRegistry.cpp` | `TargetRegistry` 与 `Target` 的实现：链表注册、`lookupTarget`、`createMCObjectStreamer`/`createAsmStreamer` 工厂。 |
| `llvm/include/llvm/MC/TargetRegistry.h` | `Target`（POD，装满构造函数指针）与 `TargetRegistry`（静态接口）、各 `RegisterXxx` 模板。 |
| `llvm/tools/llvm-mc/llvm-mc.cpp` | `llvm-mc` 工具：手搓 `MCContext`+各类 MC 子系统，按 `-filetype` 选汇编流或对象流，演示 MC 层的最小驱动。 |
| `llvm/lib/Target/X86/MCTargetDesc/X86MCTargetDesc.cpp` | X86 后端注册其全部 MC 组件的范例。 |
| `llvm/lib/Target/X86/TargetInfo/X86TargetInfo.cpp` | X86 用 `RegisterTarget<Triple::x86,...>` 把自己挂进全局链表的范例。 |

## 4. 核心概念与源码讲解

### 4.1 MC 层全景：从 MachineInstr 到目标文件

#### 4.1.1 概念说明

后端流水线（u6-l1）的终点是「发射」。但 `MachineInstr` 是面向后端优化的表示，**不能直接写进文件**。原因有二：

1. 它还停留在「虚拟/物理寄存器 + 抽象操作码」的层面，没有变成目标 CPU 真正的字节编码；
2. 一个目标文件不只是「一串指令字节」，还要包含符号表、重定位表、段（section）、调试信息、unwind 信息等结构化内容。

MC 层就是为此而设的「翻译 + 打包」层。它的输入是 `MCInst`（一条目标的机器指令），输出是两种产物之一：

- **目标汇编 `.s`**：人类可读的汇编文本，可再交给系统汇编器（`as`）；
- **目标文件 `.o`**：直接可链接的二进制（ELF/COFF/Mach-O/...）。

注意 `MCInst` 与 `MachineInstr` 不是一回事：`MachineInstr` 属于 MIR（后端 IR，可被各种 pass 改写）；`MCInst` 属于 MC 层（更接近最终编码，结构更轻）。后端的 **AsmPrinter** 负责 `MachineInstr → MCInst` 的「降级」，然后把 `MCInst` 喂给 MC 层的发射器 `MCStreamer`。在 [llvm/include/llvm/CodeGen/AsmPrinter.h:L101-L106](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/AsmPrinter.h#L101-L106) 可以看到 AsmPrinter 持有的正是这两个对象：

- `OutContext`：一个 `MCContext`（MC 层的上下文）；
- `OutStreamer`：一个 `MCStreamer`（发射器）。

而 [llvm/include/llvm/CodeGen/AsmPrinter.h:L361](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/AsmPrinter.h#L361) 的 `EmitToStreamer(MCStreamer &S, const MCInst &Inst)` 就是「把降级后的 `MCInst` 推进流里」的统一入口——它是 MIR 与 MC 两层的分界线。

#### 4.1.2 核心流程

从源码视角，一次「发射」可归纳为：

```text
AsmPrinter 把 MachineInstr 降级为 MCInst
        │ EmitToStreamer(OutStreamer, Inst)
        ▼
   MCStreamer.emitInstruction(MCInst)        ← 抽象接口
        │
        ├── 若 OutStreamer 是 MCAsmBaseStreamer（汇编流）
        │       → 调 MCInstPrinter 把 MCInst 打印成汇编文本
        │
        └── 若 OutStreamer 是 MCObjectStreamer（对象流）
                → emitInstToData：MCCodeEmitter 编码成字节
                → 记录 fixup（待修正地址）
                → finishImpl()：布局 + MCObjectWriter 写目标文件
```

要点：

- **同一个抽象接口、两种实现**。`MCStreamer::emitInstruction` 是虚函数，汇编流和对象流各自实现，调用方（AsmPrinter / 解析器）完全不用关心最终产物是 `.s` 还是 `.o`。这正是 u1-l4 里 `opt`/`llc`「薄壳驱动」理念在 MC 层的延续。
- **谁来创建这个 Streamer？** 由 `Target::createAsmStreamer` 或 `Target::createMCObjectStreamer` 工厂方法创建，而「选哪个」取决于工具的输出类型（`llc -filetype=asm` vs `-filetype=obj`）。
- **目标文件如何落盘？** `MCObjectStreamer` 内部持有一个 `MCAssembler`，它在 `Finish()` 里完成段布局、fixup 求解，再交 `MCObjectWriter`（ELF/COFF/...）写出。

#### 4.1.3 源码精读

`MCStreamer` 基类把「发射一条指令」「收尾」都声明为虚函数，见 [llvm/include/llvm/MC/MCStreamer.h:L1131-L1148](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/MC/MCStreamer.h#L1131-L1148)：

```cpp
/// Emit the given \p Instruction into the current section.
virtual void emitInstruction(const MCInst &Inst, const MCSubtargetInfo &STI);

/// Streamer specific finalization.
virtual void finishImpl();
/// Finish emission of machine code.
void finish(SMLoc EndLoc = SMLoc());
```

注意 `finish()` 是非虚的对外入口，内部转调虚的 `finishImpl()`——这是「模板方法」模式：公共收尾步骤（DWARF 行号表、伪探针等）由基类统一处理，真正「写文件」的差异留给子类的 `finishImpl()`。

而整个 `MCStreamer` 类的核心状态只有几样，见 [llvm/include/llvm/MC/MCStreamer.h:L222-L224](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/MC/MCStreamer.h#L222-L224)：

```cpp
class LLVM_ABI MCStreamer {
  MCContext &Context;
  std::unique_ptr<MCTargetStreamer> TargetStreamer;
  ...
```

即「一个上下文 + 一个可选的目标扩展流」。`MCTargetStreamer` 是给目标后端挂「自己的特殊指令/伪指令」用的钩子（如 ARM 的 `.fn_start`），其文档注释在 [llvm/include/llvm/MC/MCStreamer.h:L70-L94](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/MC/MCStreamer.h#L70-L94) 讲得很清楚：目标应实现 `FooTargetStreamer`（纯虚接口）+ `FooTargetAsmStreamer`（汇编实现）+ `FooTargetELFStreamer`（对象实现）三件套，调用方一律只跟基类对话。

#### 4.1.4 代码实践（源码阅读型）

**目标**：在源码里确认「AsmPrinter → MCStreamer」这条桥，并定位 MIR 与 MC 的分界。

1. 打开 `llvm/include/llvm/CodeGen/AsmPrinter.h`，找到 `OutContext`（L101）与 `OutStreamer`（L106）两个字段，确认 AsmPrinter 同时持有 MC 上下文与发射器。
2. 找到 `EmitToStreamer`（L361）的声明，再到 `llvm/lib/CodeGen/AsmPrinter/AsmPrinter.cpp` 搜索其定义，确认它最终调用 `S.emitInstruction(Inst, ...)`——这就是把 `MCInst` 交给 MC 层的瞬间。
3. 观察：`MachineInstr` 到这一步为止属于后端，`emitInstruction` 之后的所有处理都在 MC 层。

**需要观察的现象**：你会看到 AsmPrinter 在 emit 之前，会通过 `MCInstLowering`（每个目标自有的「降低器」）把 `MachineInstr` 翻译成 `MCInst`；翻译完才 `EmitToStreamer`。MIR 与 MC 的边界就在这一「翻」之间。

**预期结果**：能用自己的话指出「`MachineInstr` 是后端 IR，`MCInst` 是 MC 层 IR，`EmitToStreamer` 是两者的传送带」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LLVM 不直接把 `MachineInstr` 写进目标文件，而要经一层 `MCInst`？

> 参考答案：`MachineInstr` 面向后端优化（携带大量元信息、可被 pass 反复改写），结构与目标文件所需的「紧凑字节编码 + 段/重定位结构」不匹配；`MCInst` 是面向「编码与序列化」的轻量表示。分层后，后端优化与机器码发射各自演进、互不耦合，且 `MCInst` 还能被汇编器（解析 `.s`）与反汇编器（解析字节）共用，形成「IR ↔ 文本 ↔ 字节」的对称往返。

**练习 2**：`finish()` 与 `finishImpl()` 为何要拆成两个？

> 参考答案：`finish()` 是固定入口（模板方法），负责所有格式都需要的公共收尾（DWARF 行表、伪探针等）；`finishImpl()` 是虚函数，让汇编流/对象流各写自己的「真正落盘」逻辑。这样公共流程只实现一次，差异点被隔离在子类。

### 4.2 MCContext：机器代码的上下文与符号/段唯一化

#### 4.2.1 概念说明

`MCContext` 是整个 MC 层的「大脑/容器」。它集中持有：

- 目标 triple、汇编信息（`MCAsmInfo`）、寄存器信息（`MCRegisterInfo`）、子目标信息（`MCSubtargetInfo`）、对象文件信息（`MCObjectFileInfo`）；
- 符号表（`MCSymbol` 的集合）；
- 各对象格式的「段唯一化表」（保证同名段只有一个 `MCSection` 对象）；
- 诊断回调、DWARF 相关状态等。

可以把 `MCContext` 类比为前端 IR 的 `LLVMContext`（u3-l3 提到类型在 `LLVMContext` 内唯一化）：它也是**唯一化的场所**——同一个名字的符号、同一个段，在整个发射过程中只存在一个对象，比较时只需比指针。

#### 4.2.2 核心流程

`MCContext` 在构造时做的第一件大事是：**依据 triple 选定对象格式环境**。这一选择决定了后续段表用哪一张、符号用哪一个具体子类（`MCSymbolELF`/`MCSymbolMachO`/`MCSymbolCOFF`/...）。

```text
triple.getObjectFormat()  →  MCContext::Env 取下列之一
        IsELF / IsCOFF / IsMachO / IsWasm / IsXCOFF / IsGOFF / IsSPIRV / IsDXContainer
```

之后，任何「按名取符号」「按名取段」的请求，都经 `getOrCreateSymbol` / 对应 uniquing map 查表，命中则返回旧对象，未命中则新建并登记。

#### 4.2.3 源码精读

`MCContext` 构造函数在 [llvm/lib/MC/MCContext.cpp:L65-L119](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/MC/MCContext.cpp#L65-L119)，其核心是一个对 `TheTriple.getObjectFormat()` 的大 `switch`：

```cpp
MCContext::MCContext(const Triple &TheTriple, const MCAsmInfo &mai,
                     const MCRegisterInfo &mri, const MCSubtargetInfo &msti,
                     const SourceMgr *mgr, bool DoAutoReset,
                     StringRef Swift5ReflSegmentName)
    : ... {
  ...
  switch (TheTriple.getObjectFormat()) {
  case Triple::MachO: Env = IsMachO; break;
  case Triple::COFF:
    if (!TheTriple.isOSWindowsOrUEFI())
      reportFatalUsageError("cannot initialize MC for non-Windows COFF ...");
    Env = IsCOFF; break;
  case Triple::ELF:  Env = IsELF;  break;
  case Triple::Wasm: Env = IsWasm; break;
  ...
  }
}
```

> 中文说明：这段在构造期就锁定「本模块按哪种目标文件格式来组织」，并为 COFF 做了「仅支持 Windows/UEFI」的合法性校验。

各格式的段唯一化表作为成员并列存在，见 [llvm/include/llvm/MC/MCContext.h:L315-L321](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/MC/MCContext.h#L315-L321)：

```cpp
StringMap<MCSectionMachO *> MachOUniquingMap;
std::map<COFFSectionKey, MCSectionCOFF *> COFFUniquingMap;
StringMap<MCSectionELF *> ELFUniquingMap;
std::map<WasmSectionKey, MCSectionWasm *> WasmUniquingMap;
std::map<XCOFFSectionKey, MCSectionXCOFF *> XCOFFUniquingMap;
...
```

> 中文说明：每种对象格式一张表，按段名（必要时加 flags/entry size 组合键）去重；`switch` 选定的 `Env` 决定了后续查询走哪一张。

符号的去重入口是 [llvm/include/llvm/MC/MCContext.h:L482](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/MC/MCContext.h#L482) 的 `getOrCreateSymbol`：

```cpp
MCSymbol *getOrCreateSymbol(const Twine &Name);
```

> 中文说明：有就返回旧的，没有就建一个并登记——和 `LLVMContext` 里类型的「结构相同即同一对象」是同一思路，使得符号判等只需比指针。

#### 4.2.4 代码实践（源码阅读型）

**目标**：亲眼看到 `MCContext` 是怎么被工具一行行「组装」出来的。

1. 打开 `llvm/tools/llvm-mc/llvm-mc.cpp`，定位 [llvm/tools/llvm-mc/llvm-mc.cpp:L494-L497](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/tools/llvm-mc/llvm-mc.cpp#L494-L497)：

   ```cpp
   MCContext Ctx(TheTriple, *MAI, *MRI, *STI, &SrcMgr);
   std::unique_ptr<MCObjectFileInfo> MOFI(
       TheTarget->createMCObjectFileInfo(Ctx, PIC, LargeCodeModel));
   Ctx.setObjectFileInfo(MOFI.get());
   ```

2. 对照前面的 [L449-L454](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/tools/llvm-mc/llvm-mc.cpp#L449-L454)，确认 `MRI`、`MAI` 都是先用 `TheTarget->createMCRegInfo / createMCAsmInfo` 造出来的——即 MC 各子系统全由 `Target` 工厂方法创建，再塞进 `MCContext`。

**需要观察的现象**：`MCContext` 本身只是个容器，它持有的「能力」全部来自外部传入的各 MC 子系统对象。

**预期结果**：理解「`MCContext` = triple + 一组 MC 子系统 + 符号表 + 段表」，以及它与 `Target` 工厂的衔接关系。**待本地验证**：若你已构建 LLVM，可在 `llvm-mc.cpp` 给 `MCContext` 构造之后加一行 `errs() << Ctx.isELF();`，分别用 ELF 与 Mach-O triple 跑一次，观察输出差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么段要「唯一化」？如果不唯一化会怎样？

> 参考答案：目标文件里同名段在布局时必须合并成一段。若内存里存在两个 `.text` 对象，发射时就无法统一计算偏移、重定位也会指错地方。唯一化保证「同名同属性段 = 同一对象」，布局与符号解析才有唯一答案。

**练习 2**：`MCContext` 在构造时校验了 COFF 的什么？为什么？

> 参考答案：校验 `TheTriple.isOSWindowsOrUEFI()`，因为 LLVM 的 COFF 流只支持 Windows/UEFI 场景；否则直接 `reportFatalUsageError` 拒绝继续，避免后续按错误的格式假设去生成。

### 4.3 MCStreamer：流式发射器（汇编流 vs 对象流）

#### 4.3.1 概念说明

`MCStreamer` 是 MC 层对外的统一发射接口，但它是一个**抽象类**，真正干活的是它的两个分支：

- **汇编流（AsmStreamer / `MCAsmBaseStreamer`）**：把每条 `MCInst` 经 `MCInstPrinter` 打印成汇编文本，产物是 `.s`。轻量、可读。
- **对象流（`MCObjectStreamer` 及其子类）**：把 `MCInst` 编码成字节、记录 fixup，最后布局写出 `.o`。`MCObjectStreamer` 是「各格式对象流」的共同中段，再往下细分为 `MCELFStreamer`、`MCWinCOFFStreamer`、`MCWasmStreamer`、`MCGOFFStreamer`、`MCXCOFFStreamer`、`MCSPIRVStreamer`、`MCDXContainerStreamer` 等。

`MCObjectStreamer` 内部持有一个 `MCAssembler`，后者又组合了三件套：`MCAsmBackend`（后端，处理 fixup/松弛）、`MCCodeEmitter`（把 `MCInst` 编码成字节）、`MCObjectWriter`（写具体文件格式）。

#### 4.3.2 核心流程

对象流发射一条指令的关键决策在 [llvm/lib/MC/MCObjectStreamer.cpp:L401-L435](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/MC/MCObjectStreamer.cpp#L401-L435)：

```text
emitInstruction(Inst):
  if 需要松弛? (Backend.mayNeedRelaxation(...))
     若 RelaxAll: 就地反复松弛到稳定，再 emitInstToData
     否则:        emitInstToFragment（把指令放进可变长 fragment）
  else
     emitInstToData（直接编码进数据 fragment）
```

`emitInstToData` 的核心是「编码 + 记 fixup」，见 [llvm/lib/MC/MCObjectStreamer.cpp:L437-L473](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/MC/MCObjectStreamer.cpp#L437-L473)：

```cpp
void MCObjectStreamer::emitInstToData(const MCInst &Inst, const MCSubtargetInfo &STI) {
  MCFragment *F = getCurrentFragment();
  size_t CodeOffset = getCurFragSize();
  SmallString<16> Content;
  SmallVector<MCFixup, 1> Fixups;
  getAssembler().getEmitter().encodeInstruction(Inst, Content, Fixups, STI);
  appendContents(Content);
  ...
  F->appendFixups(Fixups);
}
```

> 中文说明：`MCCodeEmitter::encodeInstruction` 一边把指令编码成字节 `Content`，一边把它里面「还无法确定地址」的部分作为 `MCFixup` 报上来；字节追加进当前 fragment，fixup 记在 fragment 上，留待布局后求解。

最后由 `finishImpl` 收尾，见 [llvm/lib/MC/MCObjectStreamer.cpp:L789-L803](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/MC/MCObjectStreamer.cpp#L789-L803)：

```cpp
void MCObjectStreamer::finishImpl() {
  getContext().RemapDebugPaths();
  if (getContext().getGenDwarfForAssembly()) MCGenDwarfInfo::Emit(this);
  MCDwarfLineTable::emit(this, getAssembler().getDWARFLinetableParams());
  MCPseudoProbeTable::emit(this);
  getAssembler().Finish();   // ← 布局 + 求解 fixup + MCObjectWriter 写盘
}
```

#### 4.3.3 源码精读

`MCObjectStreamer` 的构造函数把这些组件拼装起来，见 [llvm/lib/MC/MCObjectStreamer.cpp:L28-L41](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/MC/MCObjectStreamer.cpp#L28-L41)：

```cpp
MCObjectStreamer::MCObjectStreamer(MCContext &Context,
                                   std::unique_ptr<MCAsmBackend> TAB,
                                   std::unique_ptr<MCObjectWriter> OW,
                                   std::unique_ptr<MCCodeEmitter> Emitter)
    : MCStreamer(Context),
      Assembler(std::make_unique<MCAssembler>(
          Context, std::move(TAB), std::move(Emitter), std::move(OW))),
      ... {
  IsObj = true;
  ...
}
```

> 中文说明：对象流「带着一个 `MCAssembler`」，而 `MCAssembler` 把后端、编码器、写文件器三件套打包。这是「组合优于继承」的典型——发射策略由注入的三件套决定，而非由类层次决定。

类的整体定义与职责说明见 [llvm/include/llvm/MC/MCObjectStreamer.h:L33-L44](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/MC/MCObjectStreamer.h#L33-L44)：

```cpp
/// Streaming object file generation interface.
class LLVM_ABI MCObjectStreamer : public MCStreamer {
  std::unique_ptr<MCAssembler> Assembler;
  ...
```

它 override 了基类的 `emitInstruction`（[L130](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/MC/MCObjectStreamer.h#L130)）与 `finishImpl`（[L192](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/MC/MCObjectStreamer.h#L192)），从而把抽象接口落实到「编码 + 写文件」。

#### 4.3.4 代码实践

**目标**：用 `llvm-mc` 切换 `-filetype`，观察「同一段汇编 → 汇编流 vs 对象流」两种产物的差别，并对照 `llvm-mc.cpp` 的分支。

1. 准备一个最小汇编文件 `tiny.s`（X86 AT&T 语法，示例代码）：

   ```asm
       .text
       .globl _start
   _start:
       movl $42, %eax
       retq
   ```

2. 产出**汇编流**（重新打印汇编），对应 `llvm-mc.cpp` 的 [L622-L635](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/tools/llvm-mc/llvm-mc.cpp#L622-L635)（`createAsmStreamer` 分支）：

   ```bash
   llvm-mc -triple=x86_64-linux-gnu -filetype=asm tiny.s -o tiny.print.s
   ```

3. 产出**对象流**（真正的 ELF `.o`），对应 [L646-L655](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/tools/llvm-mc/llvm-mc.cpp#L646-L655)（`createMCObjectStreamer` 分支）：

   ```bash
   llvm-mc -triple=x86_64-linux-gnu -filetype=obj tiny.s -o tiny.o
   file tiny.o            # 应识别为 ELF 64-bit
   ```

**需要观察的现象**：`-filetype=asm` 走的是 `createAsmStreamer`（带 `MCInstPrinter`，打印文本）；`-filetype=obj` 走的是 `createMCObjectStreamer`（构造 `MCAsmBackend`+`MCCodeEmitter`+`MCObjectWriter`），输出二进制。两者共用同一个 `MCContext`、同一套解析器，区别仅在 streamer 不同。

**预期结果**：你应当亲眼看到，仅靠「换一个 streamer」，同一份输入就能分别得到 `.s` 与 `.o`——这正是「统一抽象接口、两种实现」的价值。**待本地验证**：上述命令需要先按 u1-l3 构建 LLVM。

#### 4.3.5 小练习与答案

**练习 1**：`MCObjectStreamer` 为什么要在内部组合一个 `MCAssembler`，而不是把布局/编码逻辑直接写在 streamer 里？

> 参考答案：`MCAssembler` 封装了「段布局 + fixup 求解 + 三件套（backend/emitter/writer）」这套与具体格式无关的通用流程；各格式 streamer（ELF/COFF/...）只需补充自己特有的伪指令与写文件细节。这样通用流程只实现一次、可被所有对象流复用，职责清晰。

**练习 2**：什么情况下一条指令会走 `emitInstToFragment` 而不是 `emitInstToData`？

> 参考答案：当 `Backend.mayNeedRelaxation(...)` 为真（即这条指令的长度可能因地址变化而改变，典型如变长跳转）且未开启 `RelaxAll` 时，会走 `emitInstToFragment`，把指令放进一个独立的「可松弛 fragment」，留到 `MCAssembler::Finish()` 的松弛迭代中确定最终编码。

### 4.4 TargetRegistry：目标后端的统一注册表

#### 4.4.1 概念说明

LLVM 支持 dozens 种目标后端（X86、ARM、AArch64、RISC-V、MIPS、WebAssembly...），但 `llvm-mc`、`llc`、`clang` 这些工具的代码并不为每种后端写一份分支。奥秘在 `TargetRegistry`：它维护一张**全局链表**，每个后端在程序启动时把自己「挂」上去，工具凭 triple「按架构」或凭名字查表，拿到一个 `Target` 对象——后者其实是一个装满了「构造函数指针」的 POD。

关键设计选择：

- **避免静态构造**：`Target` 是 POD，由后端提供零初始化的全局实例，再在显式的 `LLVMInitializeXxxTargetInfo()` 函数里注册。这样可以按需链接、按需初始化（只初始化用得到的目标），减小二进制体积与启动开销。
- **能力即函数指针**：`Target` 是否支持 JIT、是否支持对象生成、是否支持汇编解析，分别由对应构造函数指针是否非空来表示（`hasJIT()`/`hasMCAsmBackend()`/...）。

#### 4.4.2 核心流程

```text
启动阶段（每个后端 .cpp 里）：
   全局 Target TheXxxTarget;                      // 零初始化 POD
   LLVMInitializeXxxTargetInfo()  → RegisterTarget 把它挂进链表
   LLVMInitializeXxxTargetMC()    → RegisterMCInstrInfo / RegisterMCRegInfo / ...

运行阶段（工具里）：
   Triple TT(...);
   const Target *T = TargetRegistry::lookupTarget(TT, Err);   // 遍历链表找 ArchMatchFn 命中者
   T->createMCRegInfo(TT);  T->createMCCodeEmitter(...);  ...  // 按需调用各工厂
   T->createMCObjectStreamer(TT, Ctx, Backend, Writer, Emitter, STI);  // 据 TT.getObjectFormat() 选 ELF/COFF/...
```

`lookupTarget` 有两个重载：一个只给 triple，遍历链表找第一个 `ArchMatchFn` 命中的（且要求唯一）；另一个先按架构名查，再回填 triple。

#### 4.4.3 源码精读

链表头与注册逻辑在 [llvm/lib/MC/TargetRegistry.cpp:L25-L26](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/MC/TargetRegistry.cpp#L25-L26) 与 [L180-L202](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/MC/TargetRegistry.cpp#L180-L202)：

```cpp
static Target *FirstTarget = nullptr;
...
void TargetRegistry::RegisterTarget(Target &T, const char *Name, ...) {
  if (T.Name) return;          // 允许重复注册，幂等
  T.Next = FirstTarget;        // 头插法
  FirstTarget = &T;
  T.Name = Name; T.ShortDesc = ShortDesc;
  T.BackendName = BackendName;
  T.ArchMatchFn = ArchMatchFn;
  T.HasJIT = HasJIT;
}
```

> 中文说明：所有目标串成单链表，`FirstTarget` 是头；注册就是「头插」。幂等检查 `if (T.Name) return;` 让重复初始化无害。

`lookupTarget(triple)` 在 [L153-L178](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/MC/TargetRegistry.cpp#L153-L178)，核心是用 `ArchMatchFn` 遍历链表，并额外检查「是否有两个目标都匹配」（歧义则报错）：

```cpp
const Target *TargetRegistry::lookupTarget(const Triple &TT, std::string &Error) {
  if (targets().begin() == targets().end()) {
    Error = "Unable to find target ... (no targets are registered)";
    return nullptr;
  }
  Triple::ArchType Arch = TT.getArch();
  auto ArchMatch = [&](const Target &T) { return T.ArchMatchFn(Arch); };
  auto I = find_if(targets(), ArchMatch);
  ...
}
```

> 中文说明：「没有任何目标注册」会给出非常明确的错误信息——这通常是因为工具忘了调用 `InitializeAllTargets()` 或只链接了部分后端。

`Target::createMCObjectStreamer` 是「选格式」的工厂，在 [llvm/lib/MC/TargetRegistry.cpp:L36-L91](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/MC/TargetRegistry.cpp#L36-L91)：

```cpp
MCStreamer *Target::createMCObjectStreamer(const Triple &T, MCContext &Ctx, ...) {
  MCStreamer *S = nullptr;
  switch (T.getObjectFormat()) {
  case Triple::COFF:    S = COFFStreamerCtorFn(...);      break;
  case Triple::MachO:   S = MachOStreamerCtorFn ? ... : createMachOStreamer(...); break;
  case Triple::ELF:     S = ELFStreamerCtorFn  ? ... : createELFStreamer(...);     break;
  case Triple::Wasm:    S = createWasmStreamer(...);      break;
  ...
  }
  if (ObjectTargetStreamerCtorFn) ObjectTargetStreamerCtorFn(*S, STI);
  return S;
}
```

> 中文说明：`Target` 根据对象格式选对应的 streamer 构造函数；若目标注册了「自定义 ELF/COFF streamer」（很多后端会注册，以便注入特有伪指令），就用自定义的，否则用默认的 `createELFStreamer` 等。最后再挂上目标的 `MCTargetStreamer`。

`Target` 类本身是一个装满构造函数指针的 POD，定义在 [llvm/include/llvm/MC/TargetRegistry.h:L148](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/MC/TargetRegistry.h#L148)，前面 L154–L220 列出了一长串 `using XxxCtorFnTy = ...` 的函数指针类型（`MCAsmInfo`/`MCRegInfo`/`TargetMachine`/`AsmPrinter`/`MCCodeEmitter`/各 streamer...）。其「能力查询」就是判这些指针是否非空，例如 [L391-L397](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/MC/TargetRegistry.h#L391-L397)：

```cpp
bool hasTargetMachine() const { return TargetMachineCtorFn != nullptr; }
bool hasMCAsmBackend()  const { return MCAsmBackendCtorFn != nullptr; }
bool hasMCAsmParser()   const { return MCAsmParserCtorFn != nullptr; }
```

而 `createTargetMachine`（[L478-L487](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/MC/TargetRegistry.h#L478-L487)）就是直接转调该函数指针。`RegisterTarget` 模板（[L1068-L1080](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/MC/TargetRegistry.h#L1068-L1080)）把「模板参数里的 `ArchType`」固化进一个 `getArchMatch` 函数，免去手写：

```cpp
template <Triple::ArchType TargetArchType = Triple::UnknownArch, bool HasJIT = false>
struct RegisterTarget {
  RegisterTarget(Target &T, const char *Name, const char *Desc, const char *BackendName) {
    TargetRegistry::RegisterTarget(T, Name, Desc, BackendName, &getArchMatch, HasJIT);
  }
  static bool getArchMatch(Triple::ArchType Arch) { return Arch == TargetArchType; }
};
```

X86 后端就是这套机制的样板。先在 [llvm/lib/Target/X86/TargetInfo/X86TargetInfo.cpp:L14-L28](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/X86/TargetInfo/X86TargetInfo.cpp#L14-L28) 注册两个 `Target` 全局实例（32/64 位）：

```cpp
Target &llvm::getTheX86_32Target() { static Target TheX86_32Target; return TheX86_32Target; }
Target &llvm::getTheX86_64Target() { static Target TheX86_64Target; return TheX86_64Target; }

extern "C" LLVM_C_ABI void LLVMInitializeX86TargetInfo() {
  RegisterTarget<Triple::x86,    /*HasJIT=*/true> X(
      getTheX86_32Target(), "x86", "32-bit X86: Pentium-Pro and above", "X86");
  RegisterTarget<Triple::x86_64, /*HasJIT=*/true> Y(
      getTheX86_64Target(), "x86-64", "64-bit X86: EM64T and AMD64", "X86");
}
```

再在 [llvm/lib/Target/X86/MCTargetDesc/X86MCTargetDesc.cpp:L805-L854](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/X86/MCTargetDesc/X86MCTargetDesc.cpp#L805-L854) 的 `LLVMInitializeX86TargetMC` 里把一整套 MC 组件挂上去：

```cpp
extern "C" LLVM_C_ABI void LLVMInitializeX86TargetMC() {
  for (Target *T : {&getTheX86_32Target(), &getTheX86_64Target()}) {
    RegisterMCAsmInfoFn X(*T, createX86MCAsmInfo);
    TargetRegistry::RegisterMCInstrInfo(*T, createX86MCInstrInfo);
    TargetRegistry::RegisterMCRegInfo(*T, createX86MCRegisterInfo);
    TargetRegistry::RegisterMCSubtargetInfo(*T, X86_MC::createX86MCSubtargetInfo);
    TargetRegistry::RegisterMCCodeEmitter(*T, createX86MCCodeEmitter);
    ...
    TargetRegistry::RegisterCOFFStreamer(*T, createX86WinCOFFStreamer);
    TargetRegistry::RegisterELFStreamer(*T, createX86ELFStreamer);
    TargetRegistry::RegisterMCInstPrinter(*T, createX86MCInstPrinter);
  }
  TargetRegistry::RegisterMCAsmBackend(getTheX86_32Target(), createX86_32AsmBackend);
  TargetRegistry::RegisterMCAsmBackend(getTheX86_64Target(), createX86_64AsmBackend);
}
```

> 中文说明：注意 X86 同时注册了「自定义 COFF streamer」与「自定义 ELF streamer」——所以上一节 `createMCObjectStreamer` 里 `COFFStreamerCtorFn`/`ELFStreamerCtorFn` 非空，会用 X86 自己的版本（注入 `.seh_*` 等 X86/Windows 特有伪指令的处理）。

工具侧（`llvm-mc`）的使用入口在 [llvm/tools/llvm-mc/llvm-mc.cpp:L264-L282](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/tools/llvm-mc/llvm-mc.cpp#L264-L282)，它调用带架构名的 `lookupTarget(ArchName, TheTriple, Error)`；而程序最前面 [L385-L388](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/tools/llvm-mc/llvm-mc.cpp#L385-L388) 先初始化所有后端：

```cpp
llvm::InitializeAllTargetInfos();
llvm::InitializeAllTargetMCs();
llvm::InitializeAllAsmParsers();
llvm::InitializeAllDisassemblers();
```

> 中文说明：这组 `InitializeAll*` 是 `llvm/Support/TargetSelect.h` 提供的「一把全注册」宏；如果只想注册部分后端，可用 `LLVM_INITIALIZE_ALL...` 的精细版本（详见 u9-l4 添加新后端）。

#### 4.4.4 代码实践

**目标**：用 `-version` 直接「看到」注册表里的全部目标，并验证 `lookupTarget` 的按架构匹配。

1. 列出所有已注册目标（对应 [L209-L227](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/MC/TargetRegistry.cpp#L209-L227) 的 `printRegisteredTargetsForVersion`）：

   ```bash
   llvm-mc --version | grep -A30 "Registered Targets"
   ```

2. 故意给一个不存在的架构名，触发 `lookupTarget` 的报错路径：

   ```bash
   llvm-mc -arch=nope -filetype=asm tiny.s   # 预期：invalid target 'nope'.
   ```

3. 用 triple 让 `lookupTarget(triple)` 选定目标，再跑一次对象流：

   ```bash
   llvm-mc -triple=aarch64-linux-gnu -filetype=obj tiny.s -o tiny_arm64.o
   file tiny_arm64.o    # 应为 ELF 64-bit, ARM aarch64
   ```

**需要观察的现象**：第 1 步打印的「Registered Targets」列表，正是 `FirstTarget` 链表经 `targets()` 遍历排序后的结果；第 2 步走的是 `lookupTarget` 里 `find_if` 找不到的分支（[L124-L130](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/MC/TargetRegistry.cpp#L124-L130)）。

**预期结果**：你应当确信——工具本身不知道 X86/AArch64 的存在，它只是在一张「启动期填好的表」上查名字。**待本地验证**：上述命令需先构建 LLVM；若只构建了部分 `LLVM_TARGETS_TO_BUILD`（u1-l3），`Registered Targets` 列表会相应缩短。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Target` 设计成 POD，而不是带虚函数的普通类？

> 参考答案：为了避免静态构造带来的初始化顺序问题与启动开销。POD 全局实例零初始化是确定行为；真正「填字段」由显式的 `LLVMInitializeXxxTarget*` 函数在 main 里完成。这让工具可以按需只链接、只初始化需要的后端，也方便做 ABI 边界（`extern "C" LLVM_C_ABI`）。

**练习 2**：`createMCObjectStreamer` 里的 `COFFStreamerCtorFn` 为空时会怎样？

> 参考答案：COFF 没有默认 streamer（`assert(T.isOSWindowsOrUEFI()...)` 且必须由目标注册），若目标没注册 `COFFStreamerCtorFn`，该函数指针为空、调用会出问题；而 ELF/MachO 有默认 `createELFStreamer`/`createMachOStreamer` 兜底。因此一个想支持 Windows 的新后端必须注册自己的 COFF streamer。

**练习 3**：`lookupTarget(triple)` 发现「两个目标都能匹配同一个架构」时如何处理？

> 参考答案：它在 [L170-L175](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/MC/TargetRegistry.cpp#L170-L175) 继续向后找第二个匹配；若找到，说明架构歧义，返回错误并列出两个目标名，要求用户用 `-arch` 显式指定。

## 5. 综合实践

把本讲三个最小模块（MCContext / MCStreamer / TargetRegistry）串起来，完成一次「手工驱动的 MC 往返」。

**任务**：分别用 X86（ELF）和 AArch64（ELF）两条链路，把同一段汇编变成目标文件，再反汇编回来，对照源码解释全过程。

1. 准备 `tiny.s`（示例代码，见 4.3.4）。
2. 汇编为对象文件（`createMCObjectStreamer` 选 `createELFStreamer`）：

   ```bash
   llvm-mc -triple=x86_64-linux-gnu    -filetype=obj tiny.s -o tiny_x86.o
   llvm-mc -triple=aarch64-linux-gnu   -filetype=obj -mattr=+sve tiny.s -o tiny_arm64.o 2>/dev/null || true
   ```

   > 说明：AArch64 默认语法/寄存器与 X86 不同，这里只是演示换 triple 后 `lookupTarget` 会选到不同 `Target`；若汇编不兼容可只做 X86 这一路。

3. 反汇编观察字节（`MCDisassembler` 把字节解码回 `MCInst`，再由 `MCInstPrinter` 打印——与发射方向相反，但复用同一套 `MCInstrInfo`）：

   ```bash
   llvm-objdump -d tiny_x86.o
   ```

4. **对照源码回答**：
   - 你的 `llvm-mc` 在 [llvm-mc.cpp:L385-L388](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/tools/llvm-mc/llvm-mc.cpp#L385-L388) 注册了哪些后端？（用 `--version` 印证）
   - [L494-L497](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/tools/llvm-mc/llvm-mc.cpp#L494-L497) 构造的 `MCContext`，依据你的 triple 把 `Env` 设成了什么（`IsELF`）？
   - [L646-L655](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/tools/llvm-mc/llvm-mc.cpp#L646-L655) 调 `createMCObjectStreamer`，最终在 [TargetRegistry.cpp:L57-L64](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/MC/TargetRegistry.cpp#L57-L64) 命中 `Triple::ELF` 分支，对吗？

**预期结果**：你能用一句话说清「triple → `lookupTarget` 选 `Target` → `createMCObjectStreamer` 据 object format 选 ELF 流 → `MCObjectStreamer` 编码并写盘」，并理解反汇编是这条链的反向复用。**待本地验证**：所有命令均需先按 u1-l3 构建 LLVM。

## 6. 本讲小结

- MC 层是后端的「最后一公里」：把后端 IR（`MachineInstr`）经 AsmPrinter 降级为 `MCInst`，再由 MC 层发射成目标汇编或目标文件；`EmitToStreamer` 是 MIR 与 MC 的分界线。
- `MCContext` 是 MC 层的容器与唯一化场所：构造时按 triple 锁定对象格式环境（`IsELF`/`IsCOFF`/...），并集中持有各 MC 子系统、符号表与各格式的段去重表。
- `MCStreamer` 是统一的流式发射抽象，分两支：汇编流（打印文本）与对象流（`MCObjectStreamer`，编码字节 + 记 fixup，经 `MCAssembler::Finish` 布局落盘）。调用方只对抽象接口编程，不关心产物是 `.s` 还是 `.o`。
- `TargetRegistry` 用一张全局单链表注册所有后端：每个后端在启动期把一个装满构造函数指针的 POD `Target` 挂进链表；工具凭 triple「按架构」查表拿到 `Target`，再按需调工厂方法创建 MC 各组件。
- `Target::createMCObjectStreamer` 依据 `Triple.getObjectFormat()` 选择 ELF/COFF/Mach-O/Wasm 等具体流，并允许后端注册自定义流以注入目标特有伪指令（X86 即范例）。

## 7. 下一步学习建议

- **u6-l5（TableGen 与目标描述）**：MC 层大量复用的 `MCInstrInfo`、`MCCodeEmitter`、`MCRegisterInfo` 其实都由 TableGen 的 `.td` 描述自动生成。学完本讲后再看 TableGen，能立刻把这些「工厂方法背后」的对象与 `.td` 一一对应。
- **u9-l4（添加一个新后端）**：那里会完整演示「新建一个 `Target` 并用 `RegisterTarget` + `RegisterMC*` 把它接入 MC 层」的全过程，是本讲 TargetRegistry 知识的实战出口。
- **延伸阅读**：若对「字节 ↔ 指令」的解码侧感兴趣，可阅读 `llvm/lib/MC/MCDisassembler/MCDisassembler.cpp` 与各目标的 `AsmBackend`、`MCCodeEmitter`，它们与本讲的发射方向互为镜像，复用同一套 `MCInstrInfo`。

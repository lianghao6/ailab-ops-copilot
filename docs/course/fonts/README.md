# 随仓库分发的字体

这些第三方字体资产不适用根项目的 [MIT 许可](../../../LICENSE)。四个 TTF
均使用 SIL Open Font License 1.1（OFL）；本目录包含完整版权声明与许可证正文，
不是摘要或只有许可链接。许可保留字体的使用、嵌入、修改与分发条件；使用这些
字体产生的教材 PDF 文档本身不因字体而适用 OFL。

| 资产 | 字体版权与许可 |
|---|---|
| `NotoSansSC.ttf` | © 2014–2021 Adobe；保留字体名 `Source`；[完整 OFL](OFL-NotoSansSC.txt) |
| `NotoSansSC-Regular.ttf` | 同上，项目生成的 wght=400 静态实例；[完整 OFL](OFL-NotoSansSC.txt) |
| `JetBrainsMono.ttf` | Copyright 2020 The JetBrains Mono Project Authors；[完整 OFL](OFL-JetBrainsMono.txt) |
| `JetBrainsMono-Bold.ttf` | 同上；[完整 OFL](OFL-JetBrainsMono.txt) |

## 许可来源与核对

许可文本于 2026-10-08 从权威发布方取得：

- Noto Sans SC：[Google Fonts 官方仓库该字体的 OFL.txt](https://github.com/google/fonts/blob/main/ofl/notosanssc/OFL.txt)。版权与保留字体名与两个本地 Noto TTF 的 name ID 0 一致；name ID 13/14 也标明 OFL 1.1。
- JetBrains Mono：[JetBrains 官方字体仓库 OFL.txt](https://github.com/JetBrains/JetBrainsMono/blob/master/OFL.txt)。版权与两个本地 JetBrains TTF 的 name ID 0 一致；name ID 13/14 标明 OFL 1.1。

完整保留上游版权、前言、定义、五项条件、终止条款和免责声明，仅去除行尾空格。
本地文本 SHA-256（以 LF 与无行尾空格形式）：

```text
babcfe66c8a098b2fa279bc724a3a342f8124f77ce18941fbcc1bbb39823cded OFL-NotoSansSC.txt
c1ab7c666206842a02b35b30770dac0d7a10156ed401c9defc3f02a754d89e90 OFL-JetBrainsMono.txt
```

既有三个原始 TTF 的历史下载地址和上游提交未在仓库记录，不能据此声称它们
与今天上游二进制逐字节一致。本次核对的是字体自身版权/许可元数据和权威
许可证全文，没有臆造字体版本或新增权利人。许可资产的完整性由出版 QA 校验。

## 本项目的字体修改

`NotoSansSC-Regular.ttf` 于 2026-10-08 用 fontTools 4.66.1 从仓库已有的
`NotoSansSC.ttf` 实例化 wght=400，使用 `--update-name-table` 更新静态字重名。
保留原 Unicode、版权和许可元数据；没有使用保留字体名 `Source`，不声称修改
得到 Adobe、Google 或 JetBrains 背书。转换命令见 [课程构建说明](../README.md)。
其他三个字体文件未在本次出版任务中修改；Bold 虽未用于当前正文，也随仓库
分发，因此一并补齐 JetBrains Mono 完整许可。字体及其派生版本继续使用 OFL。

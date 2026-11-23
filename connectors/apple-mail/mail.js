// Письма из Почты macOS (входящие всех ящиков, в том числе Exchange). Только чтение:
// письма не открываются в интерфейсе и не помечаются прочитанными.
// Использование: osascript -l JavaScript mail.js [часов_назад=16] [символов_текста=500] [inbox|sent] [фильтр]
// фильтр: регулярное выражение по отправителю и теме (без учёта регистра); с ним скрипт быстрее,
// потому что текст писем, не подошедших под фильтр, не читается.
// Вывод: JSON-массив писем, новые сверху.
function localIso(d) {
  const p = n => String(n).padStart(2, '0');
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? '+' : '-';
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}` +
    `${sign}${p(Math.floor(Math.abs(off) / 60))}:${p(Math.abs(off) % 60)}`;
}

// Третий аргумент `sent` читает отправленные вместо входящих.
function run(argv) {
  const hoursBack = Number(argv[0] || 16);
  const bodyChars = Number(argv[1] || 500);
  const folder = argv[2] === 'sent' ? /^(sent|sent messages|sent items|отправленные)$/i : /^(inbox|входящие)$/i;
  const since = new Date(Date.now() - hoursBack * 3600 * 1000);
  const filter = argv[3] ? new RegExp(argv[3], 'i') : null;
  const Mail = Application('Mail');
  const out = [];
  Mail.accounts().forEach(acc => {
    let boxes = [];
    try {
      boxes = acc.mailboxes().filter(b => folder.test(b.name()));
    } catch (e) { return; }
    boxes.forEach(box => {
      let msgs = [];
      const dateField = argv[2] === 'sent' ? 'dateSent' : 'dateReceived';
      try { msgs = box.messages.whose({ [dateField]: { _greaterThan: since } })(); } catch (e) { return; }
      msgs.forEach(m => {
        if (filter) {
          let head = '';
          try { head = m.sender() + ' ' + m.subject(); } catch (e) {}
          if (!filter.test(head)) return;
        }
        let body = '';
        try { body = (m.content() || '').replace(/\s+/g, ' ').trim().slice(0, bodyChars); } catch (e) {}
        let to = [], cc = [];
        try { to = m.toRecipients().map(r => r.address()); } catch (e) {}
        try { cc = m.ccRecipients().map(r => r.address()); } catch (e) {}
        out.push({
          account: acc.name(),
          received: localIso(argv[2] === 'sent' ? m.dateSent() : m.dateReceived()),
          from: m.sender(),
          to, cc,
          subject: m.subject(),
          read: m.readStatus(),
          flagged: m.flaggedStatus(),
          replied: m.wasRepliedTo(),
          body,
        });
      });
    });
  });
  out.sort((a, b) => b.received.localeCompare(a.received));
  return JSON.stringify(out, null, 2);
}

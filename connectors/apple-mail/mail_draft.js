// Черновики ответов в Почте macOS. Ничего не отправляет: письмо сохраняется в «Черновики» ящика
// (Exchange синхронизирует их в Outlook и на телефон), отправляет владелец сам.
// Использование: osascript -l JavaScript mail_draft.js <вход.json>
// Вход: {"items":[{"account":"Exchange","to":["a@b.ru"],"cc":[],"subject":"Re: …","body":"…"}]}
// Пропускает, если в черновиках ящика уже есть письмо с той же темой. Не больше MAX за запуск.
ObjC.import('Foundation');
const MAX = 5;
const DRAFTS = /^(drafts|черновики)$/i;

function run(argv) {
  const raw = $.NSString.stringWithContentsOfFileEncodingError(argv[0], $.NSUTF8StringEncoding, null);
  if (!raw || raw.isNil()) return JSON.stringify({ error: 'Не прочитан вход: ' + argv[0] });
  const items = (JSON.parse(ObjC.unwrap(raw)).items || []).slice(0, MAX);
  const Mail = Application('Mail');
  const saved = [], skipped = [];
  items.forEach(it => {
    const subject = (it.subject || '').trim(), body = (it.body || '').trim();
    const to = (it.to || []).filter(Boolean);
    if (!subject || !body || !to.length) { skipped.push({ subject, why: 'нет темы, текста или адресата' }); return; }
    let acc;
    try { acc = it.account ? Mail.accounts.byName(it.account) : Mail.accounts()[0]; acc.name(); }
    catch (e) { skipped.push({ subject, why: 'нет ящика ' + (it.account || '') }); return; }
    const box = acc.mailboxes().find(b => DRAFTS.test(b.name()));
    if (!box) { skipped.push({ subject, why: 'в ящике нет папки черновиков' }); return; }
    if (box.messages.whose({ subject: subject })().length) { skipped.push({ subject, why: 'черновик с этой темой уже есть' }); return; }
    const msg = Mail.OutgoingMessage({ subject, content: body, visible: false, sender: acc.emailAddresses()[0] });
    Mail.outgoingMessages.push(msg);
    to.forEach(a => msg.toRecipients.push(Mail.Recipient({ address: a })));
    (it.cc || []).filter(Boolean).forEach(a => msg.ccRecipients.push(Mail.Recipient({ address: a })));
    try {
      msg.save();
      saved.push({ account: acc.name(), subject });
    } catch (e) {
      skipped.push({ subject, why: 'не сохранилось: ' + e.message });
    }
    try { Mail.delete(msg); } catch (e) {} // убрать скрытое окно письма; сохранённый черновик остаётся
  });
  return JSON.stringify({ drafts_saved: saved, skipped }, null, 2);
}

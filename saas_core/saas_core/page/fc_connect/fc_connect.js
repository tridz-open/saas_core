frappe.pages["fc-connect"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("FC Connect"), single_column: true });
	const $b = $(`<div class="fc-banner"></div><div class="fc-status"></div>`).appendTo(page.body);
	const call = (m, args) => frappe.call({ method: `saas_core.saas_core.page.fc_connect.fc_connect.${m}`, args }).then((r) => r.message);
	const refresh = () => call("status").then((s) => {
		$b.filter(".fc-banner").html(s.banner ? `<div class="alert alert-danger">${frappe.utils.escape_html(s.banner)}</div>` : "");
		$b.filter(".fc-status").html(`<pre>${frappe.utils.escape_html(JSON.stringify(s, null, 2))}</pre>`);
	});
	page.add_field({ fieldname: "email", fieldtype: "Data", label: __("FC email") });
	page.add_field({ fieldname: "code", fieldtype: "Data", label: __("6-digit code") });
	page.set_primary_action(__("Verify"), () =>
		call("verify", { code: page.fields_dict.code.get_value(), email: page.fields_dict.email.get_value() })
			.then(() => { frappe.show_alert(__("Connected")); refresh(); }));
	page.set_secondary_action(__("Send code"), () =>
		call("send_code", { email: page.fields_dict.email.get_value() }).then(() => frappe.show_alert(__("Code sent"))));
	page.add_inner_button(__("Refresh"), refresh);
	refresh();
};

"use strict";

// This file contains a series of small customizations to the HTML to
// improve the appearance of our docs.

$(document).ready(function() {
	// use some jQuery to find all the <#include> hints and tag them with a CSS class so we can style them appropriately
	$('p > code:contains("#include")').addClass("include-hint");

	// find paragraphs of "Inherited/Reimplemented by <long list of classes>" and make them collapsible
	$('div.contents > p').filter(function () { return this.textContent.startsWith('Inherited by '); }).each(function () {
		$(this).wrap('<details></details>');
		$(this).parent().prepend('<summary>Inherited by</summary>');
	});
	$('div.memdoc > p').filter(function () { return this.textContent.startsWith('Reimplemented in '); }).each(function () {
		$(this).wrap('<details></details>');
		$(this).parent().prepend('<summary>Reimplemented in</summary>');
	});
	$('div.memdoc > p').filter(function () { return this.textContent.startsWith('Implemented in '); }).each(function () {
		$(this).wrap('<details></details>');
		$(this).parent().prepend('<summary>Implemented in</summary>');
	});
	$('div.memdoc > p').filter(function () { return this.innerHTML.startsWith('Implements <a class="el"'); }).each(function () {
		$(this).wrap('<i></i>');
	});

	// Make class/namespace names in the header title appear in monospace font
	$('div.headertitle > div.title').each(function () {
		this.innerHTML = this.innerHTML.replace(/ImFusion::([\w:]+)( \w+ Reference)/, '<span style="font-family: var(--font-family-monospace)">$1</span>$2');
	});

	// if HIDE_SCOPE_NAMES is off in Doxyfile, the symbols are enumerated with their fully-qualified name
	// we remove the `ImFusion::` namespace part since it is implicitly clear and only clutters the output
	$('td.memItemRight > a, td.memItemRight > b, td.memname')
		.filter(function () { return this.innerText.includes('ImFusion::'); })
		.each(function () { this.innerText = this.innerText.replace('ImFusion::', ''); });

	// add link to our support forums
	let mainMenu = document.getElementById('main-menu');
	let li = document.createElement('li');
	li.setAttribute('style', 'float: right');
	li.innerHTML += '<a href="https://forum.imfusion.com" target="_blank">Support Forums</a>';
	mainMenu.appendChild(li);

	// add monospace font style to symbol references (heuristic part 1, for namespace/class/struct pages)
	let htmlFilename = location.pathname.substring(location.pathname.lastIndexOf("/") + 1);
	if (['namespace_', 'class_', 'struct_'].some(function(str) { return htmlFilename.startsWith(str);}))
	{
		let pageNavCustomCss = document.createElement('style');
		pageNavCustomCss.innerText = `#page-nav span:not([style*='0px']) ~ a { font-family: var(--font-family-monospace) !important; }`;
		document.head.appendChild(pageNavCustomCss);
	}
});

// use a MutationObserver to get notified when the side navbars are populated
// Newer versions of Doxygen use fully qualified symbol names making them very long
// We remove the 'ImFusion::' prefix because this is the ImFusion SDK and obviously everything is in the ImFusion namespace
let navTreeObserver = new MutationObserver(function(mutations) {
	mutations.forEach(function(mutation) {
		if (mutation.addedNodes) {
			mutation.addedNodes.forEach(function(node) {
				if (node.nodeName.toLowerCase() == 'span'
						&& node.parentElement.nodeName.toLowerCase() == 'a'
						&& node.parentElement.parentElement.className.includes('label')
						&& node.innerText.startsWith('ImFusion::')) {
					node.innerText = node.innerText.replace('ImFusion::', '');
				}
			});
		}
	});
});

let groupSectionsWereAlreadyAddedToPageNav = false;
let pageNavObserver = new MutationObserver(function(mutations) {
	mutations.forEach(function(mutation) {
		if (mutation.addedNodes) {
			// Vanilla doxygen does not show sections of module/group pages in the page nav -> add them manually
			let htmlFilename = location.pathname.substring(location.pathname.lastIndexOf("/") + 1);
			if (htmlFilename.startsWith('group__') && !groupSectionsWereAlreadyAddedToPageNav) {
				groupSectionsWereAlreadyAddedToPageNav = true; // need this to avoid infinite recursion

				let insertBeforeNode = document.querySelector('#nav-header-details').nextSibling;
				$('div.contents > h1.doxsection').each(function () {
					let sectionName = this.innerText;
					let anchorName = this.getElementsByClassName('anchorlink')[0].getAttribute('href');
					let itemNode = $('<li/>').append(
										$('<div/>').addClass('item')
											.append($('<span/>').addClass('arrow').attr('style', 'padding-left: 16px;'))
											.append($('<a/>').attr('href', anchorName).text(sectionName))
										)
					insertBeforeNode.parentNode.insertBefore(itemNode[0], insertBeforeNode);
				});
			}

			// remove 'ImFusion::' prefix
			$(mutation.addedNodes)
				.find("a")
				.each(function () {
					this.innerText = this.innerText.replace('ImFusion::', '');
				});

			// add monospace font style to symbol references (heuristic part 2, for group/module pages)
			let subsequentBlockHaveSymbols = false;
			$(mutation.addedNodes)
				.find("li")
				.each(function () {
					let isTopLevelEntry = $(this).find('span')[0].style['padding-left'] == '0px';
					if (isTopLevelEntry && !subsequentBlockHaveSymbols) {
						let sectionName = $(this).find('a')[0].innerText;
						const sectionsWithSymbols = ['Classes', 'Enumerations', 'Functions', 'Macros', 'Namespaces', 'Typedefs', 'Enumeration Type Documentation', 'Function Documentation', 'Macro Definition Documentation', 'Typedef Documentation'];
						subsequentBlockHaveSymbols = sectionsWithSymbols.some(function (str) { return sectionName == str; });
					}
					else if (!isTopLevelEntry && subsequentBlockHaveSymbols) {
						$(this).find('a')[0].style['font-family'] = 'var(--font-family-monospace)';
					}
				});
		}
	});
});
navTreeObserver.observe(document.getElementById('nav-tree-contents'), {childList: true, subtree: true});
pageNavObserver.observe(document.getElementById('page-nav-contents'), {childList: true, subtree: true});

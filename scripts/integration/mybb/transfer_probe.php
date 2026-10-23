<?php
/** Controlled compatibility plugin. Original MyBB still validates its CSRF token.
 * Moves the token source to metadata and renames the submitted parameter.
 * No validation, permission, flood, moderation or authentication check is disabled.
 */
if (!defined('IN_MYBB')) die('Direct access denied');
$plugins->add_hook('global_start', 'poster_transfer_input');
$plugins->add_hook('pre_output_page', 'poster_transfer_output');
function poster_transfer_input() {
    global $mybb;
    if (isset($mybb->input['vouch'])) $mybb->input['my_post_key'] = $mybb->input['vouch'];
}
function poster_transfer_output($page) {
    if (defined('THIS_SCRIPT') && THIS_SCRIPT === 'newthread.php') {
        $page = preg_replace_callback('/<input\b(?=[^>]*\bname="my_post_key")[^>]*>/i', function($m) {
            preg_match('/value="([^"]*)"/', $m[0], $value);
            return '<meta name="proof-source" content="'.($value[1] ?? '').'">';
        }, $page);
        $page .= '<script>document.addEventListener("submit",e=>{
          const source=e.target.querySelector("meta[name=proof-source]");
          if(source){const input=document.createElement("input");input.type="hidden";
            input.name="vouch";input.value=source.content;e.target.append(input);}
        },true);</script>';
    }
    return $page;
}

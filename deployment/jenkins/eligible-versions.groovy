// Paste into the UI's JAR_VERSION Active Choices Reactive Parameter.
// Referenced parameters: APP,DEPLOYMENT_TRACK,ACTION. Choice type: Single Select.
// This trusted controller script needs administrator review/Script Approval.
// It only reads the published catalog; deploy.py enforces promotion policy.
import com.cloudbees.plugins.credentials.CredentialsProvider
import com.cloudbees.plugins.credentials.common.StandardUsernamePasswordCredentials
import com.cloudbees.plugins.credentials.domains.URIRequirementBuilder
import groovy.json.JsonSlurper
import hudson.model.Item
import hudson.security.ACL
import java.nio.charset.StandardCharsets
import java.util.Base64

final String nexusUrl = 'https://REPLACE_ME_NEXUS_HOST'
final String recordsRepository = 'odm-deployments'
final String credentialId = 'odm-catalog-reader'
final String selectPrompt = 'Select a version:selected'
def connection = null
try {
    String app = binding.hasVariable('APP') ? APP.toString() : ''
    String track = binding.hasVariable('DEPLOYMENT_TRACK') ? DEPLOYMENT_TRACK.toString() : ''
    String action = binding.hasVariable('ACTION') ? ACTION.toString() : 'DEPLOY'
    boolean validTrack = track ==~ /[A-Za-z0-9][A-Za-z0-9_.-]{0,63}/
    if (!(app in ['ESERV', 'WACON']) || !validTrack || !(action in ['DEPLOY', 'ROLLBACK'])) {
        return [selectPrompt]
    }
    def owner = binding.hasVariable('jenkinsProject') ? jenkinsProject : null
    if (!(owner instanceof Item) || nexusUrl.contains('REPLACE_ME')) {
        return ['Version list is unavailable:selected']
    }
    String suffix = action == 'ROLLBACK' ? '-rollback' : ''
    URL url = new URL("${nexusUrl}/repository/${recordsRepository}/catalogs/${track}/${app}${suffix}.json")
    if (url.protocol != 'https' || url.userInfo || url.query || url.ref) {
        return ['Version list is unavailable:selected']
    }
    def credentials = CredentialsProvider.lookupCredentialsInItem(
        StandardUsernamePasswordCredentials.class, owner, ACL.SYSTEM2,
        URIRequirementBuilder.fromUri(nexusUrl).build()
    ).find { it.id == credentialId }
    if (!credentials) { return ['Version list is unavailable:selected'] }
    connection = url.openConnection()
    connection.instanceFollowRedirects = false
    connection.connectTimeout = 5000
    connection.readTimeout = 5000
    connection.setRequestProperty('Accept', 'application/json')
    String token = Base64.encoder.encodeToString(
        (credentials.username + ':' + credentials.password.plainText).getBytes(StandardCharsets.UTF_8)
    )
    connection.setRequestProperty('Authorization', 'Basic ' + token)
    if (connection.responseCode != 200 || connection.contentLengthLong > 2097152L) {
        return ['Version list is unavailable:selected']
    }
    byte[] bytes
    connection.inputStream.withCloseable { stream -> bytes = stream.readNBytes(2097153) }
    if (bytes.length > 2097152) { return ['Version list is unavailable:selected'] }
    def catalog = new JsonSlurper().parseText(new String(bytes, StandardCharsets.UTF_8))
    if (catalog.schema_version != 1 || catalog.app != app || catalog.track != track || catalog.action != action || !(catalog.versions instanceof List)) {
        return ['Version list is unavailable:selected']
    }
    def versions = catalog.versions.findAll {
        it.version instanceof String && it.version ==~ /[A-Za-z0-9][A-Za-z0-9._-]{0,79}/ &&
        !(it.version.toLowerCase() in ['latest', 'highest', 'select']) &&
        !it.version.toUpperCase().contains('SNAPSHOT') && it.sha256 ==~ /[a-f0-9]{64}/
    }.collect { it.version }.unique()
    return [selectPrompt] + versions
} catch (Exception ignored) {
    // Do not expose credentials, internal responses or stack traces in the form.
    return ['Version list is unavailable:selected']
} finally {
    if (connection != null) { connection.disconnect() }
}
